from datetime import datetime
from io import BytesIO
import re
import argparse
import tempfile
import os
import subprocess
import shutil

from httpx import Client
from PIL import Image
from rich import print, prompt


def parse_agouti_url(url: str) -> tuple[str, str]:
    # https://agouti.eu/project/e1271e4e-c63a-41ca-a730-74286b3e8984/annotate/sequence/3e19712c-630a-42b5-a13e-7d7067bd236d

    regex = r"https://agouti\.eu/project/([a-f0-9-]+)/annotate/sequence/([a-f0-9-]+)"
    match = re.match(regex, url)
    if not match:
        raise ValueError("Invalid Agouti URL format")
    project_id, sequence_id = match.groups()
    return project_id, sequence_id


def parse_agouti_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create an animation from an Agouti sequence")
    parser.add_argument("--username", help="Agouti username/email")
    parser.add_argument("--password", help="Agouti password")
    parser.add_argument("--sequence-id", help="Agouti sequence UUID")
    parser.add_argument("--project-id", help="Agouti project UUID (used for output naming)")
    parser.add_argument("--format", choices=["gif", "webp", "mp4"], dest="output_ext", help="Animation format")
    return parser


def main() -> None:
    args = build_parser().parse_args()

    print("I can build an animation from an Agouti sequence.")

    username = args.username or prompt.Prompt.ask("Agouti username/email")
    password = args.password or prompt.Prompt.ask("Agouti password", password=True)
    output_ext = args.output_ext or prompt.Prompt.ask("Output format", choices=["gif", "webp", "mp4"], default="webp")

    project_id = args.project_id
    sequence_id = args.sequence_id

    if sequence_id:
        if not project_id:
            project_id = prompt.Prompt.ask("Project ID (used in output filename)")
    else:
        url = prompt.Prompt.ask("Agouti URL")
        try:
            project_id, sequence_id = parse_agouti_url(url)
            print(f"Project ID: {project_id}")
            print(f"Sequence ID: {sequence_id}")
        except ValueError as e:
            print(f"[red]Error:[/red] {e}")
            return

    from agoutix import Agouti

    agouti = Agouti(
        username,
        password,
    )

    client = Client()

    client.headers["Authorization"] = f"Bearer {agouti.token}"
    response = client.get(f"https://api.agouti.eu/sequences/{sequence_id}?include=assets%2Cobservations%2Cobservations.createdBy%2Cdeployment")

    json = response.json()

    assets = [thing for thing in json['included'] if thing['type'] == 'assets']

    assets.sort(
        key=lambda asset: (
            parse_agouti_timestamp(asset['attributes']['created-at']),
            asset['attributes']['original-filename'],
        )
    )
    asset_ids = [asset['id'] for asset in assets]

    if not asset_ids:
        print("[red]No assets found for this sequence.[/red]")
        return

    timestamps = [parse_agouti_timestamp(asset['attributes']['created-at']) for asset in assets]
    # Use a static 200 ms interval for every frame (4x speed-up fixed)
    frame_durations_ms = [200] * len(timestamps)

    frames: list[Image.Image] = []
    base_size: tuple[int, int] | None = None

    for index, asset_id in enumerate(asset_ids):
        image_raw, filename = agouti.get_asset_file(asset_id)
        with Image.open(BytesIO(image_raw)) as image:
            frame = image.convert("RGB")
            if base_size is None:
                base_size = frame.size
            elif frame.size != base_size:
                frame = frame.resize(base_size)
            frames.append(frame)
        print(f"Loaded image {index + 1} from {filename}")

    output_filename = f"p{project_id}_s{sequence_id}.{output_ext}"

    if output_ext == "mp4":
        if shutil.which("ffmpeg") is None:
            print("[red]ffmpeg not found on PATH — install ffmpeg or choose webp/gif[/red]")
            return
        # Save frames to a temporary directory as PNGs
        with tempfile.TemporaryDirectory() as tmpdir:
            for idx, frame in enumerate(frames):
                path = os.path.join(tmpdir, f"frame_{idx:06d}.png")
                frame.save(path, format="PNG")
            # Calculate framerate from static interval (200 ms -> 5 fps)
            fps = max(1, int(round(1000 / 200)))
            cmd = [
                "ffmpeg",
                "-y",
                "-framerate",
                str(fps),
                "-i",
                os.path.join(tmpdir, "frame_%06d.png"),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "23",
                output_filename,
            ]
            try:
                subprocess.run(cmd, check=True)
            except subprocess.CalledProcessError as e:
                print(f"[red]ffmpeg failed: {e}[/red]")
                return
        print(f"[green]Saved animation to {output_filename}[/green]")
    else:
        frames[0].save(
            output_filename,
            save_all=True,
            append_images=frames[1:],
            duration=frame_durations_ms,
            loop=0,
        )
        print(f"[green]Saved animation to {output_filename}[/green]")
    