from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "docs" / "assets"
OUTPUT = ASSET_DIR / "pr-mcp-builder-demo.gif"
CANVAS_SIZE = (960, 720)
HEADER_HEIGHT = 76
FOOTER_HEIGHT = 34
SCREEN_AREA = (0, HEADER_HEIGHT, CANVAS_SIZE[0], CANVAS_SIZE[1] - FOOTER_HEIGHT)


STEPS = [
    ("1. 기관 선택", "readme-guide-01-start.png", None),
    ("2. 규정 파일 업로드", "readme-guide-02-upload.png", None),
    ("3. 전처리 진행률 확인", "readme-guide-02-progress.png", None),
    ("4. AI 제안과 사람 검수", "readme-guide-04-approval-actions.png", None),
    ("5. ChatGPT Desktop 연결 대상 선택", "readme-guide-05-mcp-next.png", (300, 330, 1440, 1000)),
    ("6. MCP 이름과 저장 위치 확인", "readme-guide-06-bundle.png", (300, 0, 1440, 1000)),
    ("7. MCP 생성 진행률 확인", "readme-guide-06-progress.png", (300, 0, 1440, 1000)),
]


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/malgunbd.ttf" if bold else "C:/Windows/Fonts/malgun.ttf"),
        Path("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf" if bold else "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _fit_screenshot(path: Path, crop: tuple[int, int, int, int] | None) -> Image.Image:
    image = Image.open(path).convert("RGB")
    if crop is not None:
        image = image.crop(crop)
    width = SCREEN_AREA[2] - SCREEN_AREA[0]
    height = SCREEN_AREA[3] - SCREEN_AREA[1]
    return ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS)


def _step_frame(
    title: str,
    image_name: str,
    crop: tuple[int, int, int, int] | None,
    index: int,
    total: int,
) -> Image.Image:
    frame = Image.new("RGB", CANVAS_SIZE, "#F6F8FA")
    draw = ImageDraw.Draw(frame)
    draw.rectangle((0, 0, CANVAS_SIZE[0], HEADER_HEIGHT), fill="#123B33")
    draw.rectangle((0, HEADER_HEIGHT - 5, CANVAS_SIZE[0], HEADER_HEIGHT), fill="#E5B93F")
    draw.text((30, 20), title, font=_font(30, bold=True), fill="white")
    screenshot = _fit_screenshot(ASSET_DIR / image_name, crop)
    frame.paste(screenshot, (SCREEN_AREA[0], SCREEN_AREA[1]))
    draw.rectangle(
        (0, CANVAS_SIZE[1] - FOOTER_HEIGHT, CANVAS_SIZE[0], CANVAS_SIZE[1]),
        fill="#FFFFFF",
    )
    draw.text((24, CANVAS_SIZE[1] - 27), "PR MCP Builder · 사용 흐름", font=_font(17), fill="#334155")
    progress = f"{index}/{total}"
    progress_width = draw.textlength(progress, font=_font(17, bold=True))
    draw.text(
        (CANVAS_SIZE[0] - progress_width - 24, CANVAS_SIZE[1] - 27),
        progress,
        font=_font(17, bold=True),
        fill="#0F766E",
    )
    return frame


def _call_frame(total: int) -> Image.Image:
    frame = Image.new("RGB", CANVAS_SIZE, "#F6F8FA")
    draw = ImageDraw.Draw(frame)
    draw.rectangle((0, 0, CANVAS_SIZE[0], HEADER_HEIGHT), fill="#123B33")
    draw.rectangle((0, HEADER_HEIGHT - 5, CANVAS_SIZE[0], HEADER_HEIGHT), fill="#E5B93F")
    draw.text((30, 20), f"{total}. 설치 후 MCP 이름으로 호출", font=_font(30, bold=True), fill="white")
    draw.rounded_rectangle((70, 155, 890, 550), radius=12, fill="white", outline="#CBD5E1", width=2)
    draw.text((110, 200), "ChatGPT Desktop 또는 Claude를 다시 시작한 뒤", font=_font(24), fill="#334155")
    draw.text((110, 250), "새 대화에 아래 문장을 입력합니다.", font=_font(24), fill="#334155")
    draw.rounded_rectangle((110, 330, 850, 430), radius=8, fill="#EEF6F3", outline="#0F766E", width=2)
    draw.text(
        (138, 360),
        "sample_public_regulations MCP를 사용해서\n등록된 규정 목록을 보여줘.",
        font=_font(24, bold=True),
        fill="#123B33",
        spacing=8,
    )
    draw.text((110, 475), "같은 이름으로 재생성하면 추가·개정 청크가 갱신됩니다.", font=_font(20), fill="#475569")
    draw.rectangle((0, CANVAS_SIZE[1] - FOOTER_HEIGHT, CANVAS_SIZE[0], CANVAS_SIZE[1]), fill="white")
    draw.text((24, CANVAS_SIZE[1] - 27), "PR MCP Builder · 사용 흐름", font=_font(17), fill="#334155")
    progress = f"{total}/{total}"
    progress_width = draw.textlength(progress, font=_font(17, bold=True))
    draw.text(
        (CANVAS_SIZE[0] - progress_width - 24, CANVAS_SIZE[1] - 27),
        progress,
        font=_font(17, bold=True),
        fill="#0F766E",
    )
    return frame


def main() -> None:
    total = len(STEPS) + 1
    frames = [
        _step_frame(title, image_name, crop, index, total)
        for index, (title, image_name, crop) in enumerate(STEPS, start=1)
    ]
    frames.append(_call_frame(total))
    palette_frames = [
        frame.convert("P", palette=Image.Palette.ADAPTIVE, colors=128, dither=Image.Dither.NONE)
        for frame in frames
    ]
    palette_frames[0].save(
        OUTPUT,
        save_all=True,
        append_images=palette_frames[1:],
        duration=[2100] * (len(palette_frames) - 1) + [3200],
        loop=0,
        optimize=True,
        disposal=2,
    )
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
