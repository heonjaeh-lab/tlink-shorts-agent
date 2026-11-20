#!/usr/bin/env python3
"""Resolve a case's route live, optionally rendering a licensed, silent map insert."""

import argparse
import bisect
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API = "https://maps.apigw.ntruss.com"
WIDTH, HEIGHT, FPS = 1080, 1920, 30


def naver_client():
    key_id = os.getenv("NCP_APIGW_API_KEY_ID")
    key = os.getenv("NCP_APIGW_API_KEY")
    if not key_id or not key:
        raise ValueError("로컬 환경에 NCP_APIGW_API_KEY_ID와 NCP_APIGW_API_KEY를 설정하세요.")

    def get(endpoint, params, accept="application/json"):
        request = Request(f"{API}/{endpoint}?{urlencode(params)}", headers={"Accept": accept})
        # Keep API keys out of URLs, diagnostics and redirected requests.
        request.add_unredirected_header("x-ncp-apigw-api-key-id", key_id)
        request.add_unredirected_header("x-ncp-apigw-api-key", key)
        try:
            with urlopen(request, timeout=25) as res:
                if res.headers.get_content_type() != accept:
                    raise ValueError(f"NAVER {endpoint} 응답 형식을 확인하세요.")
                data = res.read(16 * 1024 * 1024 + 1)
        except HTTPError as error:
            raise ValueError(f"NAVER {endpoint} HTTP {error.code}. 앱의 상품 선택·인증·한도를 확인하세요.") from None
        except (TimeoutError, URLError):
            raise ValueError(f"NAVER {endpoint}에 연결할 수 없습니다. 네트워크를 확인하세요.") from None
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("NAVER 응답 크기가 허용 범위를 초과했습니다.")
        return json.loads(data) if accept == "application/json" else data

    return get


def coordinate(value):
    if not isinstance(value, (tuple, list)) or len(value) != 2 or any(isinstance(v, bool) for v in value):
        raise ValueError("좌표에는 경도와 위도가 필요합니다.")
    try:
        lon, lat = map(float, value)
    except (TypeError, ValueError):
        raise ValueError("좌표를 숫자로 입력하세요.") from None
    if not (-180 <= lon <= 180 and -85 < lat < 85):
        raise ValueError("좌표 범위 또는 경도·위도 순서를 확인하세요.")
    return lon, lat


def locate(spec, get):
    if not isinstance(spec, dict):
        raise ValueError("출발지와 도착지를 각각 입력하세요.")
    if "lon" in spec or "lat" in spec:
        return coordinate((spec.get("lon"), spec.get("lat")))
    address = spec.get("address")
    if not isinstance(address, str) or not address.strip():
        raise ValueError("도시나 상호만으로 위치를 정할 수 없습니다. 확인된 주소 또는 lon·lat이 필요합니다.")
    response = get("map-geocode/v2/geocode", {"query": address, "count": 2})
    matches = response.get("addresses", [])
    if response.get("status") != "OK" or response.get("meta", {}).get("totalCount") != 1 or len(matches) != 1:
        raise ValueError("주소를 하나로 특정할 수 없습니다. 정확한 도로명 주소나 확인된 좌표를 입력하세요.")
    return coordinate((matches[0].get("x"), matches[0].get("y")))


def fetch_route(config, get):
    start = locate(config["origin"], get)
    goal = locate(config["destination"], get)
    if start == goal:
        raise ValueError("출발지와 도착지가 같습니다.")
    response = get("map-direction/v1/driving", {
        "start": ",".join(map(str, start)), "goal": ",".join(map(str, goal)), "option": "traoptimal"
    })
    routes = response.get("route", {}).get("traoptimal")
    if response.get("code") != 0 or not routes:
        raise ValueError("NAVER Directions 5에서 운전 경로를 찾지 못했습니다.")
    path = [coordinate(p) for p in routes[0].get("path", [])]
    if len(path) < 2 or all(p == path[0] for p in path):
        raise ValueError("표시할 이동 경로가 부족합니다.")
    return path


def project(point):
    lon, lat = point
    return ((lon + 180) / 360, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2)


def map_frame(path, get):
    from PIL import Image

    # Static Map uses a logical 540 × 960 viewport with 2× pixel density.
    projected = [project(p) for p in path]
    xs, ys = zip(*projected)
    center_x = (min(xs) + max(xs)) / 2
    center_y = (min(ys) + max(ys)) / 2
    limits = [(size / span) for size, span in (
        (380, max(xs) - min(xs)), (730, max(ys) - min(ys))) if span]
    zoom = min(20, max(0, math.floor(math.log2(min(limits) / 256))))
    center_lon = 360 * center_x - 180
    center_lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * center_y))))
    raster = get("map-static/v2/raster", {
        "w": WIDTH // 2, "h": HEIGHT // 2, "center": f"{center_lon:.8f},{center_lat:.8f}",
        "level": zoom, "scale": 2, "format": "png", "maptype": "basic", "lang": "ko"
    }, "image/png")
    with Image.open(io.BytesIO(raster)) as image:
        if image.size != (WIDTH, HEIGHT):
            raise ValueError("Static Map의 고해상도 9:16 응답 크기를 확인하세요.")
        background = image.convert("RGB")
    pixels_per_world = 512 * 2 ** zoom
    points = [(WIDTH / 2 + (x-center_x)*pixels_per_world,
               HEIGHT / 2 + (y-center_y)*pixels_per_world) for x, y in projected]
    return background, points


def illustrated_frame(path, illustration, case_folder):
    from PIL import Image

    if not isinstance(illustration, dict):
        raise ValueError("map.illustration에는 자체 지도의 path와 bounds가 필요합니다.")
    bounds = illustration.get("bounds")
    name = illustration.get("path")
    if not isinstance(bounds, list) or len(bounds) != 4 or not isinstance(name, str) or not name:
        raise ValueError("자체 지도에 path와 [서, 남, 동, 북] 경계를 입력하세요.")
    west, south = coordinate(bounds[:2])
    east, north = coordinate(bounds[2:])
    if west >= east or south >= north:
        raise ValueError("자체 지도의 지리적 경계를 확인하세요.")
    left, top = project((west, north))
    right, bottom = project((east, south))
    points = [((x - left) / (right - left) * WIDTH, (y - top) / (bottom - top) * HEIGHT)
              for x, y in map(project, path)]
    if any(x < 80 or x > WIDTH-80 or y < 80 or y > HEIGHT-80 for x, y in points):
        raise ValueError("경로나 얼굴 배지가 잘립니다. 해당 위치를 포함한 넓은 자체 지도를 지정하세요.")
    with Image.open(case_folder / name) as image:
        if abs(image.width / image.height - WIDTH / HEIGHT) > 0.002:
            raise ValueError("자체 지도는 북쪽이 위인 9:16 일러스트로 준비하세요.")
        background = image.convert("RGB").resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    return background, points


def tally_lengths(points):
    clean = [points[0]]
    lengths = [0.0]
    for p in points[1:]:
        distance = math.dist(clean[-1], p)
        if distance:
            clean.append(p)
            lengths.append(lengths[-1] + distance)
    if len(clean) < 2:
        raise ValueError("지도상에서 경로의 길이가 0입니다.")
    return clean, lengths


def position(points, lengths, distance):
    segment = min(len(points) - 1, max(1, bisect.bisect_left(lengths, distance)))
    t = (distance - lengths[segment - 1]) / (lengths[segment] - lengths[segment - 1])
    a, b = points[segment-1], points[segment]
    return tuple(round(x + t * (y - x)) for x, y in zip(a, b))


def endpoint_header(background, names, route_positions):
    """Use a shared legend so neighboring pins can retain both full names."""
    from PIL import ImageDraw
    from edit_shorts import font

    entries = []
    for role, value, color in zip(("출발", "도착"), names, ("#28506b", "#e65c48")):
        if value is None or value == "":
            continue
        if not isinstance(value, str) or not value.strip() or not value.isprintable():
            raise ValueError("출발·도착 공개명은 줄바꿈 없는 문자열로 입력하세요.")
        entries.append((role, value, color))
    if not entries:
        return

    draw = ImageDraw.Draw(background)
    role_font = font(33)
    label_font = font(46, True)
    role_width = math.ceil(max(role_font.getlength(role) for role, _, _ in entries))
    # The bottom of the original map, including the provider credit and logo, stays intact.
    margin, gap, marker = 24, 18, 14
    prefix = marker + gap + role_width + gap
    box_limit = WIDTH - 160
    available = box_limit - 2*margin - prefix
    while max(label_font.getlength(name) for _, name, _ in entries) > available and label_font.size > 32:
        label_font = font(label_font.size-2, True)

    rows = []
    for role, name, color in entries:
        chunks, chunk = [], ""
        for letter in name:
            if chunk and label_font.getlength(chunk + letter) > available:
                chunks.append(chunk)
                chunk = ""
            chunk += letter
        chunks.append(chunk)
        if len(chunks) > 2:
            raise ValueError("지도 상단에 읽기 좋은 짧은 공개 지점명을 입력하세요.")
        rows.append((role, chunks, color))
    widths = [prefix + math.ceil(max(label_font.getlength(line) for line in chunks))
              for _, chunks, _ in rows]
    one_line = all(len(chunks) == 1 for _, chunks, _ in rows) and 2*margin+sum(widths)+40 <= box_limit
    row_height = label_font.size + 13
    if one_line:
        height = row_height
        width = sum(widths) + (len(rows)-1)*40
    else:
        height = row_height * sum(len(chunks) for _, chunks, _ in rows)
        width = max(widths)
    box_width = width + 2*margin
    # Prefer top center, then an adjacent clear area when the case artwork routes through it.
    candidates = [(x,y) for y in (66, 190, 330) for x in
                  ((WIDTH-box_width)//2, 80, WIDTH-box_width-80)]
    def intersections(origin):
        x, y = origin
        return sum(x-80 <= px <= x+box_width+80 and y-80 <= py <= y+height+104
                   for px, py in route_positions)
    left, top = min(candidates, key=intersections)
    bottom = top + height + 24
    draw.rounded_rectangle((left, top, left+width+2*margin, bottom), radius=22,
                           fill="#fff6e4", outline="#6b6656", width=3)
    x, y = left+margin, top+12+row_height/2
    for (role, chunks, color), width in zip(rows, widths):
        radius = marker//2
        draw.ellipse((x, y-radius, x+marker, y+radius), fill=color)
        draw.text((x+marker+gap, y), role, font=role_font, fill="#544c40", anchor="lm")
        for j, line in enumerate(chunks):
            draw.text((x+prefix, y+j*row_height), line, font=label_font, fill="#292b2e", anchor="lm")
        if one_line:
            x += width + 40
        else:
            y += row_height*len(chunks)


def draw_route(background, points, badge, seconds, target, names=()):
    from PIL import ImageDraw

    points, lengths = tally_lengths(points)
    total = lengths[-1]
    draw = ImageDraw.Draw(background)
    for start in range(0, math.ceil(total), 38):
        end = min(start + 22, total)
        dash = [position(points, lengths, start)]
        dash.extend(points[bisect.bisect_right(lengths, start):bisect.bisect_left(lengths, end)])
        dash.append(position(points, lengths, end))
        draw.line(dash, fill="#218e8b", width=9, joint="curve")
        for x, y in (dash[0], dash[-1]):
            draw.ellipse((x-4, y-4, x+4, y+4), fill="#218e8b")
    for (x, y), color in zip((points[0], points[-1]), ("#28506b", "#e65c48")):
        draw.ellipse((x-14, y-14, x+14, y+14), fill=color, outline="#fff4dd", width=4)
    sample_distances = range(0, math.ceil(total), 40)
    sampled = [position(points, lengths, d) for d in sample_distances]
    endpoint_header(background, names, [*sampled, points[-1]])

    frame_count = round(seconds * FPS)
    command = ["ffmpeg", "-v", "error", "-nostdin", "-n", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "pipe:0", "-an", "-c:v", "libx264",
               "-pix_fmt", "yuv420p", "-crf", "19", "-preset", "veryfast", "-movflags", "+faststart", str(target)]
    with subprocess.Popen(command, stdin=subprocess.PIPE) as proc:
        try:
            for index in range(frame_count):
                # Allow a short hold on both the office and the destination.
                fraction = min(1.0, max(0.0, (index / (frame_count-1) - 0.08) / 0.82))
                x, y = position(points, lengths, total * fraction)
                frame = background.copy()
                frame.paste(badge, (x - badge.width//2, y - badge.height//2), badge)
                proc.stdin.write(frame.tobytes())
        finally:
            proc.stdin.close()
        if proc.wait():
            raise ValueError("FFmpeg 지도 영상 인코딩에 실패했습니다.")


def run(case_path, output, badge_path, seconds, get=None):
    case = json.loads(case_path.read_text(encoding="utf-8"))
    if not isinstance(case, dict) or not isinstance(case.get("map", {}), dict):
        raise ValueError("사례와 map 입력은 JSON 객체여야 합니다.")
    config = dict(case.get("map", {}))
    if config.get("mode", "naver") != "naver":
        raise ValueError("이 명령은 map.mode가 naver인 새 경로에 사용합니다.")
    for key in ("origin", "destination"):
        config.setdefault(key, case.get(key))
        if not isinstance(config[key], dict):
            raise ValueError(f"map.{key}의 확인된 주소 또는 좌표가 필요합니다.")
    if not math.isfinite(seconds) or not 1 <= seconds <= 30:
        raise ValueError("지도 구간은 1~30초 사이로 지정하세요.")
    if output:
        from PIL import Image

        basis = config.get("video_use_basis")
        if not isinstance(basis, str) or not basis.strip():
            raise ValueError("지도·경로의 영상 저장과 공개를 허용하는 이용 근거를 map.video_use_basis에 기록하세요.")
        if output.suffix.lower() != ".mp4" or output.exists() or output.is_symlink() or not output.parent.is_dir():
            raise ValueError("기존 디렉터리 안의 새 MP4 파일명을 지정하세요.")
        if not shutil.which("ffmpeg") or not badge_path or not badge_path.is_file():
            raise ValueError("영상 출력에는 FFmpeg와 --badge 얼굴 PNG가 필요합니다.")
        with Image.open(badge_path) as image:
            if image.format != "PNG":
                raise ValueError("얼굴 배지는 PNG 형식으로 준비하세요.")
            badge = image.convert("RGBA")
            if not badge.getchannel("A").getbbox():
                raise ValueError("얼굴 배지가 완전히 투명합니다.")
            if badge.getchannel("A").getextrema() == (255, 255):
                raise ValueError("얼굴 배지 바깥은 투명하게 준비하세요.")
            badge = badge.crop(badge.getchannel("A").getbbox())
            scale = 150 / max(badge.size)
            badge = badge.resize((max(1, round(badge.width * scale)), max(1, round(badge.height * scale))),
                                 Image.Resampling.LANCZOS)
    get = get or naver_client()
    path = fetch_route(config, get)
    if not output:
        return {"status": "route_verified", "provider": "NAVER Directions 5"}
    provider = "NAVER Directions 5 + Static Map"
    if "illustration" in config:
        background, points = illustrated_frame(path, config["illustration"], case_path.parent)
        provider = "NAVER Directions 5 + supplied illustration"
    else:
        background, points = map_frame(path, get)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".tlink-map-") as temp:
        candidate = Path(temp) / "map.mp4"
        names = (config["origin"].get("name"), config["destination"].get("name"))
        draw_route(background, points, badge, seconds, candidate, names)
        os.link(candidate, output)
    return {"status": "map_rendered", "provider": provider,
            "output": str(output), "frames": round(seconds * FPS), "fps": FPS}


def main():
    parser = argparse.ArgumentParser(description="사례의 NAVER 경로 확인 및 무음 지도 삽입 영상 제작")
    parser.add_argument("case", type=Path)
    parser.add_argument("--output", type=Path, help="사용 범위가 확인된 경우에 쓸 새 MP4 파일")
    parser.add_argument("--badge", type=Path, help="투명 배경의 대표자 얼굴 원형 PNG")
    parser.add_argument("--seconds", type=float, default=4.0)
    args = parser.parse_args()
    try:
        result = run(args.case, args.output, args.badge, args.seconds)
    except (ValueError, OSError, ImportError) as error:
        parser.exit(1, f"지도 실행 중단: {error}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
