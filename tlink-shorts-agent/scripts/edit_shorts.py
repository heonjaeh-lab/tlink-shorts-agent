#!/usr/bin/env python3
"""Assemble an illustrated 9:16 short from a case's own artwork, footage and UI crops."""
import argparse
import json
import math
import os
from functools import lru_cache
from pathlib import Path
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFont, ImageOps

W, H, FPS = 1080, 1920, 24
INK = "#292b2e"
TEAL = "#145254"
PAPER = "#fff8eb"
OCHRE = "#dcab64"
MINT = "#e1eee4"
FIT = f"scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x2d3436,setsar=1"
ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p", "-threads", "3", "-an"]


def run(command):
    subprocess.run(command, check=True)


def ff(*args):
    run(["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", *map(str, args)])


def probe(filename):
    data = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                    "-show_entries", "stream=duration,nb_frames,r_frame_rate,width,height",
                                    "-of", "json", str(filename)])
    return json.loads(data)["streams"][0]


@lru_cache
def font(size, bold=False):
    custom = os.environ.get("TLINK_FONT_BOLD" if bold else "TLINK_FONT")
    apple = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")
    if custom:
        return ImageFont.truetype(custom, size)
    if apple.is_file():
        return ImageFont.truetype(str(apple), size, index=6 if bold else 2)
    for filename in ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else
                     "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                     "NotoSansKR-Regular.ttf"):
        try:
            return ImageFont.truetype(filename, size)
        except OSError:
            continue
    raise ValueError("Set TLINK_FONT and TLINK_FONT_BOLD to Hangul font files.")


def text(draw, x, y, value, size, fill=INK, bold=False, max_w=850):
    face = font(size, bold)
    if draw.textlength(value, font=face) > max_w:
        raise ValueError(f"Caption exceeds safe width ({max_w}px): {value}")
    draw.text((x, y), value, font=face, fill=fill, anchor="lt")


def lines(draw, pos, strings, size, step, **opts):
    for i, item in enumerate(strings):
        text(draw, pos[0], pos[1] + i * step, item, size, **opts)


def region(root, source):
    """Only the explicit bounds reach the compositing canvas; private pixels stay off it."""
    path = root / source["file"]
    with Image.open(path) as im:
        box = source["crop"]
        x1, y1, x2, y2 = box
        if not (0 <= x1 < x2 <= im.width and 0 <= y1 < y2 <= im.height):
            raise ValueError(f"Invalid UI crop in {path}")
        safe = im.crop(box).convert("RGB")
    return safe


def plate(image, draw, x, y, right, bottom, color, radius=38):
    draw.rounded_rectangle((x+6, y+10, right+6, bottom+10), radius, fill=(34,40,37,92))
    draw.rounded_rectangle((x, y, right, bottom), radius, fill=color, outline=INK, width=4)


def bubble(draw, y, right_side):
    if right_side:
        pts = [(784,y+6),(867,y-52),(858,y+9)]
    else:
        pts = [(142,y+9),(137,y-51),(215,y+7)]
    draw.polygon(pts, fill=PAPER)
    draw.line(pts[:2], fill=INK, width=4)
    draw.line(pts[1:], fill=INK, width=4)


def feature_focus(shot, root):
    feature = shot["feature"]
    im = Image.new("RGBA", (W,H), PAPER)
    draw = ImageDraw.Draw(im)
    x, right = 83, 976
    draw.rectangle((0,0,W,24),fill=TEAL)
    text(draw,x,162,shot["role"],41,TEAL,True)
    note = shot.get("disclosure", "")
    text(draw,right-draw.textlength(note,font=font(33)),165,note,33,"#68614f")
    draw.line((x,242,right,242),fill="#b9b39e",width=3)
    lines(draw,(x,308),shot["lines"],77,103,bold=True,max_w=right-x)
    plate(im,draw,x,618,right,1172,"#ffffff",35)
    draw.rounded_rectangle((x+4,622,right-4,740),30,fill=MINT)
    draw.rectangle((x+4,704,right-4,740),fill=MINT)
    text(draw,x+35,660,feature["label"],37,TEAL,True,max_w=right-x-70)
    panels = feature.get("detail_crops")
    if panels:
        if len(panels) != 2:
            raise ValueError("Split UI detail requires two crops.")
        middle = (x+right)//2
        draw.line((middle,786,middle,1114),fill="#d9cfbc",width=2)
        for panel, left, end in zip(panels, (x,middle), (middle,right)):
            ui = region(root, {"file":feature["file"],"crop":panel["crop"]})
            crop = ImageOps.contain(ui,(end-left-38,184),method=Image.Resampling.LANCZOS)
            im.paste(crop, ((left+end-crop.width)//2,1001-crop.height//2))
            text(draw,left+30,810,panel["label"],36,TEAL,True,max_w=end-left-60)
    else:
        ui = region(root,feature)
        crop = ImageOps.contain(ui,(right-x-56,370),method=Image.Resampling.LANCZOS)
        im.paste(crop,((x+right-crop.width)//2,948-crop.height//2))
    text(draw,x,1272,shot.get("example_label","상담 예시"),39,TEAL,True)
    draw.line((x,1347,right,1347),fill="#d9cfbc",width=3)
    for j,value in enumerate(shot.get("details",[])):
        text(draw,x,1399+100*j,value,43,INK,True,max_w=right-x)
    return im


def overlay_card(shot, root, output):
    if shot.get("layout") == "feature_focus":
        feature_focus(shot,root).save(output)
        return
    im = Image.new("RGBA", (W, H))
    draw = ImageDraw.Draw(im)
    feature = shot.get("feature")
    compact = shot.get("layout") == "dialogue_feature"
    if feature:
        left, right, top = 55, 972, (1040 if compact else 815)
        bottom = 1740 if compact else 1655
    else:
        left, right = 55, 972
        top, bottom = (1020, 1580) if shot["type"] == "question" else (1004, 1625)
    plate(im, draw, left, top, right, bottom, PAPER)
    bubble(draw, top, shot["type"] != "question")
    x = left + 45
    role_fill = TEAL if shot["type"] != "question" else "#86511f"
    text(draw, x, top+43, shot["role"], 37, role_fill, True)
    note = shot.get("disclosure")
    if note:
        note_font = font(29)
        text(draw, right-43-draw.textlength(note, font=note_font), top+50, note, 29, "#6b6251")
    draw.line((x,top+106,right-44,top+106), fill="#d9cfbc", width=2)
    size = (61 if compact else 65) if feature else 76
    gap = size+12 if compact else size+20
    lines(draw,(x,top+137 if compact else top+146),shot["lines"],size,gap,bold=True,max_w=right-x-42)
    if feature:
        y = top+(303 if compact else 350)
        footer = 206 if compact else 268
        draw.rounded_rectangle((x,y,right-44,y+footer),15,fill="white",outline="#c5cbbf",width=2)
        text(draw, x+23, y+22, feature["label"], 31, TEAL, True)
        ui = region(root, feature)
        crop_size = (right-x-132, 111 if compact else 164)
        width_limit = feature.get("display_width", crop_size[0])
        crop = ImageOps.contain(ui, (min(width_limit,crop_size[0]), crop_size[1]),
                                method=Image.Resampling.LANCZOS)
        cx, cy = (x+right-44)//2, y+(131 if compact else 163)
        im.paste(crop, (cx-crop.width//2,cy-crop.height//2))
        lines(draw,(x,y+footer+40),shot.get("details",[]),37,54,fill=INK,bold=True,max_w=right-x-45)
    else:
        detail_y = top+360
        if shot.get("details"):
            draw.rounded_rectangle((x,detail_y-10,right-45,bottom-41), 16, fill=MINT)
            lines(draw,(x+22,detail_y+13),shot["details"],37,55,fill=TEAL,bold=True,max_w=right-x-95)
    im.save(output)


def image_asset(path):
    return path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")


def extract(source, position, target):
    ff("-ss", f"{position:.6f}", "-i", source, "-frames:v", "1", target)
    if not target.is_file():
        raise ValueError(f"Could not read frame at {position}s in {source}")

def render_photo_result(shot, count, photo, folder, index):
    """Hold the installation photo, then fade in the outcome underneath it."""
    bg = Image.new("RGB", (W, H), PAPER)
    draw = ImageDraw.Draw(bg)
    draw.rectangle((0, 0, W, 18), fill=TEAL)
    title = shot.get("photo_title")
    if title:
        label = shot["photo_label"]
        label_width = draw.textlength(label, font=font(34))
        text(draw, 66, 109, title, 57, TEAL, True, max_w=W-170-label_width)
        text(draw, W-66-label_width, 126, label, 34, "#665f51", max_w=W-132)
    else:
        text(draw, 66, 109, shot["photo_label"], 47, TEAL, True, max_w=W-132)
    with Image.open(photo) as source:
        source = ImageOps.exif_transpose(source).convert("RGB")
        picture = ImageOps.contain(source, (W-96, 1088), Image.Resampling.LANCZOS)
    has_caption = bool(shot.get("lines"))
    image_center_y = 755 if has_caption else 920
    bg.paste(picture, ((W-picture.width)//2, image_center_y-picture.height//2))
    background = folder/f"photo-{index}.png"
    bg.save(background)

    segment = folder/f"cut-{index}.mp4"
    if not has_caption:
        ff("-loop",1,"-framerate",FPS,"-i",background,"-frames:v",count,*ENCODE,segment)
        return segment
    caption = Image.new("RGBA", (W, H))
    draw = ImageDraw.Draw(caption)
    text(draw, 66, 1395, shot["role"], 39, TEAL, True, max_w=W-132)
    lines(draw, (66, 1488), shot["lines"], 63, 94, bold=True, max_w=W-132)
    overlay = folder/f"photo-caption-{index}.png"
    caption.save(overlay)
    hold = float(shot.get("caption_delay_s", 0.75))
    if not 0 <= hold < count/FPS:
        raise ValueError("사진 설명의 시작 시점은 해당 컷의 재생 시간 안이어야 합니다.")
    transition = min(0.20, count/FPS-hold)
    filters = (f"[1:v]format=rgba,fade=t=in:st={hold}:d={transition}:alpha=1[caption];"
               "[0:v][caption]overlay=format=auto,format=yuv420p")
    ff("-loop",1,"-framerate",FPS,"-i",background,"-loop",1,"-framerate",FPS,"-i",overlay,
       "-filter_complex",filters,"-frames:v",count,*ENCODE,segment)
    return segment


def render_card(shot, count, art, base, root, folder, index, offset):
    ov = folder/f"overlay-{index}.png"
    overlay_card(shot, root, ov)
    if shot.get("layout") == "feature_focus":
        # Opaque product panels occupy the full frame and never consume another character take.
        segment = folder/f"cut-{index}.mp4"
        ff("-loop","1","-framerate",FPS,"-i",ov,"-frames:v",count,*ENCODE,segment)
        return segment
    if art:
        bg = art
    else:
        bg = folder/f"still-{index}.png"
        last_still = max(0,float(probe(base)["duration"]) - 2/FPS)
        extract(base,shot.get("source_still",last_still),bg)
    if image_asset(bg):
        # A small camera move keeps the illustrated background alive while UI text stays still.
        max_zoom = 0.022
        zoom = f"1+{max_zoom}*on/{max(1,count-1)}"
        motion = f"{FIT},zoompan=z='{zoom}':x='iw/2-iw/zoom/2':y='(ih-ih/zoom)*0.27':d={count}:s={W}x{H}:fps={FPS}"
        bg_input = ["-i", bg]
    else:
        duration = float(probe(bg)["duration"])
        requested = shot.get("qa_clip_start", offset)
        first = min(max(0, requested), max(0, duration-count/FPS))
        bg_input = ["-ss", f"{first:.6f}", "-i", bg]
        motion = f"fps={FPS},{FIT},tpad=stop_mode=clone:stop_duration={count/FPS}"
    filters = (f"[0:v]{motion}[bg];[1:v]format=rgba[caption];"
               "[bg][caption]overlay=x=0:y='22*max(0,1-t/0.18)':eval=frame:format=auto,format=yuv420p")
    segment = folder/f"cut-{index}.mp4"
    ff(*bg_input,"-i",ov,"-filter_complex",filters,"-frames:v",count,*ENCODE,segment)
    return segment


def travel_segment(path, start_frame, count, target, *, still=False):
    # Frame boundaries always belong to the 24 fps edit rate, including for VFR inputs.
    if still:
        args = ["-loop","1","-framerate",FPS,"-i",path]
    else:
        args = ["-i",path]
    filters = f"fps={FPS},trim=start_frame={start_frame},setpts=PTS-STARTPTS,{FIT}"
    ff(*args,"-vf",filters,"-frames:v",count,*ENCODE,target)
    if int(probe(target)["nb_frames"]) != count:
        raise ValueError(f"Input {path} cannot cover {count} frames from frame {start_frame}.")


def run_edit(manifest, output, qa_override=None, map_override=None, travel_override=None):
    root = manifest.resolve().parent
    cfg = json.loads(manifest.read_text(encoding="utf-8"))
    source = travel_override or root/cfg["travel_file"]
    data = probe(source)
    travel_count = round(float(data["duration"]) * FPS)
    total = round(cfg["target_seconds"]*FPS)
    before = cfg["opening"]
    after = cfg["closing"]
    if not before or not after:
        raise ValueError("Opening and closing shots are required.")
    counts = [round(s["seconds"]*FPS) for s in before]
    closing_counts = [round(s["seconds"]*FPS) for s in after[:-1]]
    last_count = total - sum(counts) - travel_count - sum(closing_counts)
    if min(*counts,*closing_counts,last_count) < 1:
        raise ValueError("The supplied travel and title durations exceed the target.")
    closing_counts.append(last_count)
    qa = qa_override or (root/cfg["qa_media"] if cfg.get("qa_media") else None)
    map_file = map_override or (root/cfg["map_media"] if cfg.get("map_media") else None)
    photo = None
    is_reference = False
    if cfg.get("site_photo"):
        item = cfg["site_photo"]
        is_reference = item.get("relation") == "reference_case"
        if item.get("approved") is not True or (not is_reference and item.get("same_site") is not True):
            raise ValueError("Confirm the photo use and its relationship to this case.")
        if is_reference and (item.get("same_site") is True or not item.get("label")):
            raise ValueError("A reference installation needs a distinct on-screen site label.")
        photo = root/item["file"]
    inputs = [manifest,source,qa,map_file,photo]
    inputs += [root/shot["feature"]["file"] for shot in before+after if shot.get("feature")]
    if any(path is not None and output.resolve() == Path(path).resolve() for path in inputs):
        raise ValueError("Choose an output distinct from every input file.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        folder = Path(tmp)
        cuts = []
        qa_offset = 0
        for i, (shot, count) in enumerate(zip(before,counts)):
            cuts.append(render_card(shot,count,qa,source,root,folder,i,qa_offset))
            qa_offset += count/FPS
        if map_file:
            a,b = cfg["map_replace_frames"]
            if not (0 <= a < b <= travel_count):
                raise ValueError("Map frame range is outside this travel clip.")
            parts = [(source,0,a),(map_file,0,b-a),(source,b,travel_count-b)]
        else:
            parts = [(source,0,travel_count)]
        for j, (path, pos, count) in enumerate(parts):
            if count:
                target = folder/f"travel-{j}.mp4"
                travel_segment(path,pos,count,target,still=image_asset(path))
                cuts.append(target)
        for j,(shot,count) in enumerate(zip(after,closing_counts),start=len(before)):
            if shot.get("layout") == "photo_result":
                if not photo:
                    raise ValueError("사진 엔딩에 사용할 설치 사진이 필요합니다.")
                shot = dict(shot, photo_label=item.get("label") or "티링크 설치 현장")
                cuts.append(render_photo_result(shot,count,photo,folder,j))
                continue
            use_photo = photo if not is_reference and shot.get("background") != "travel" else None
            cuts.append(render_card(shot,count,use_photo,source,root,folder,j,0))
        listing = folder/"cuts.txt"
        listing.write_text("".join(f"file '{p.name}'\n" for p in cuts))
        final = folder/"ready.mp4"
        # A final integer frame clock avoids container-level rounding at joins.
        ff("-f","concat","-safe","1","-i",listing,"-vf",f"settb=1/{FPS},setpts=N",
           "-r",FPS,"-frames:v",total,*ENCODE,"-movflags","+faststart",final)
        details = probe(final)
        if (int(details["nb_frames"]) != total or details["width"] != W or details["height"] != H
                or not math.isclose(float(details["duration"]), total/FPS, abs_tol=.001)):
            raise ValueError(f"Unexpected encoded format/duration: {details}")
        os.replace(final, output)
    return {"file":str(output),"frames":total,"fps":FPS,"travel_frames":travel_count,
            "qa_frames":sum(counts),"closing_frames":sum(closing_counts),"map_replaced":bool(map_file)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest",type=Path)
    parser.add_argument("output",type=Path)
    qa_group = parser.add_mutually_exclusive_group()
    qa_group.add_argument("--qa-image","--qa-video",dest="qa",type=Path)
    map_group = parser.add_mutually_exclusive_group()
    map_group.add_argument("--map-image","--map-video",dest="map_media",type=Path)
    parser.add_argument("--travel",type=Path,help="A matching, completed run→map→door animation")
    args = parser.parse_args()
    print(json.dumps(run_edit(args.manifest,args.output,args.qa,args.map_media,args.travel),
                     indent=2,ensure_ascii=False))
