import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import fal_client
import requests
import streamlit as st
from PIL import Image


st.set_page_config(
    page_title="Hotel Lobby Character Swap",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .stApp { background: #0d0d10; }
      .block-container { max-width: 1180px; padding-top: 2rem; padding-bottom: 4rem; }
      .hero {
        padding: 1.45rem 1.55rem;
        border: 1px solid rgba(255,255,255,.09);
        border-radius: 18px;
        background: linear-gradient(145deg, rgba(113,75,255,.16), rgba(255,255,255,.025));
        margin-bottom: 1.2rem;
      }
      .hero h1 { margin: 0 0 .3rem 0; font-size: 2rem; }
      .hero p { margin: 0; opacity: .74; }
      .step {
        display: inline-block;
        font-size: .78rem;
        font-weight: 700;
        letter-spacing: .05em;
        text-transform: uppercase;
        opacity: .68;
        margin-bottom: .35rem;
      }
      div[data-testid="stFileUploader"] section {
        border-radius: 14px;
        border-color: rgba(255,255,255,.14);
        background: rgba(255,255,255,.02);
      }
      div[data-testid="stAlert"] { border-radius: 12px; }
      .stButton > button, .stDownloadButton > button { border-radius: 12px; font-weight: 700; }
    </style>
    """,
    unsafe_allow_html=True,
)

HY_WU_MODEL = "fal-ai/hy-wu-edit"
DREAMACTOR_MODEL = "fal-ai/bytedance/dreamactor/v2"


def get_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError("FFmpeg is unavailable. Add imageio-ffmpeg to requirements.txt.") from exc


def save_bytes(data: bytes, suffix: str, folder: Path, name: str) -> Path:
    p = folder / f"{name}{suffix}"
    p.write_bytes(data)
    return p


def save_upload(uploaded_file, folder: Path, name: str) -> Path:
    suffix = Path(uploaded_file.name).suffix.lower() or ".bin"
    return save_bytes(uploaded_file.getvalue(), suffix, folder, name)


def upload_to_fal(path: Path) -> str:
    return fal_client.upload_file(str(path))


def download_file(url: str, out_path: Path):
    with requests.get(url, stream=True, timeout=180) as r:
        r.raise_for_status()
        with out_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def video_duration_seconds(video_path: Path) -> float | None:
    ffmpeg = get_ffmpeg()
    proc = subprocess.run(
        [ffmpeg, "-i", str(video_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    text = proc.stderr or ""
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def extract_frame(video_path: Path, time_s: float, out_path: Path):
    ffmpeg = get_ffmpeg()
    cmd = [
        ffmpeg,
        "-y",
        "-ss", f"{max(0.0, time_s):.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-vf", "scale='min(1920,iw)':-2",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError("Could not extract the selected source frame. Try an earlier timestamp.")


def normalize_dreamactor_image(input_path: Path, output_path: Path):
    """DreamActor accepts jpg/png, max 4.7MB, up to 1920x1080."""
    with Image.open(input_path) as im:
        im = im.convert("RGB")
        im.thumbnail((1920, 1080), Image.Resampling.LANCZOS)
        # Keep dimensions comfortably above the documented 480px minimum when possible.
        quality = 92
        while quality >= 65:
            im.save(output_path, "JPEG", quality=quality, optimize=True)
            if output_path.stat().st_size <= 4_500_000:
                break
            quality -= 7
    if output_path.stat().st_size > 4_700_000:
        raise RuntimeError("Prepared cast frame is still larger than DreamActor's 4.7MB limit.")


def remux_original_audio(
    generated_path: Path,
    source_path: Path,
    output_path: Path,
    source_audio_offset_s: float = 0.0,
):
    ffmpeg = get_ffmpeg()
    cmd = [ffmpeg, "-y", "-i", str(generated_path)]
    if source_audio_offset_s > 0:
        cmd += ["-ss", f"{source_audio_offset_s:.3f}"]
    cmd += [
        "-i", str(source_path),
        "-map", "0:v:0",
        "-map", "1:a:0?",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2500:])


def fingerprint(*parts) -> str:
    h = hashlib.sha256()
    for part in parts:
        if hasattr(part, "getvalue"):
            h.update(part.getvalue())
        elif isinstance(part, bytes):
            h.update(part)
        else:
            h.update(str(part).encode("utf-8"))
    return h.hexdigest()


def friendly_error(exc: Exception) -> str:
    raw = str(exc)
    low = raw.lower()
    if "content_policy_violation" in low or "partner_validation_failed" in low:
        return (
            "The selected fal model rejected one of the inputs under its content policy. "
            "This is a provider/model restriction, not a Streamlit or API-key error. "
            "Try eligible reference material or switch the input image used for the cast frame."
        )
    if "422" in low:
        return "fal returned a 422 validation error. Check the source duration, image formats, and selected inputs."
    if "401" in low or "unauthorized" in low:
        return "fal rejected the API key. Check the FAL_KEY secret in Streamlit."
    return f"fal request failed: {raw[:700]}"


def subscribe_with_status(model: str, arguments: dict, status_box, label: str):
    def on_queue_update(update):
        try:
            logs = getattr(update, "logs", None) or []
            if logs:
                message = logs[-1].get("message") if isinstance(logs[-1], dict) else str(logs[-1])
                if message:
                    status_box.write(message)
        except Exception:
            pass

    status_box.write(label)
    return fal_client.subscribe(
        model,
        arguments=arguments,
        with_logs=True,
        on_queue_update=on_queue_update,
    )


def build_cast_prompt(left_name: str, right_name: str, left_notes: str, right_notes: str, extra: str) -> str:
    return f"""Use image 1 as the BASE FRAME and preserve its composition.

Replace ONLY the performer on the viewer's LEFT in image 1 with the person from image 2 ({left_name}).
Use image 2 for that person's identity, face, hair, visible body/build and visible wardrobe.
{left_notes.strip() or 'Keep the LEFT person recognizable and visually consistent with image 2.'}

Replace ONLY the performer on the viewer's RIGHT in image 1 with the person from image 3 ({right_name}).
Use image 3 for that person's identity, face, hair, visible body/build and visible wardrobe.
{right_notes.strip() or 'Keep the RIGHT person recognizable and visually consistent with image 3.'}

Keep the exact pose, body placement, hand positions, camera angle, crop, microphone, studio/background, lighting, shadows and all non-person scene details from image 1. Do not swap left/right positions. Do not merge the two identities. Do not add people or props.

{extra.strip()}""".strip()


# Session defaults
for key, value in {
    "cast_frame_url": None,
    "cast_signature": None,
    "source_fal_url": None,
    "source_signature": None,
    "final_bytes": None,
    "final_name": "hotel_lobby_character_swap.mp4",
}.items():
    if key not in st.session_state:
        st.session_state[key] = value


st.markdown(
    """
    <div class="hero">
      <h1>Hotel Lobby Character Swap</h1>
      <p>Build a two-person cast frame from your source clip, then animate it with the source performance while preserving the original audio.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Configuration")
    env_key = os.getenv("FAL_KEY", "")
    api_key = st.text_input(
        "fal API key",
        type="password",
        placeholder="Uses FAL_KEY from Streamlit Secrets if left blank",
        help="Do not put your fal key in GitHub. Store it in Streamlit Secrets as FAL_KEY.",
    )
    if env_key:
        st.success("FAL_KEY detected in environment")
    else:
        st.warning("No FAL_KEY detected. Add it in Streamlit Secrets or paste it above for this session.")

    st.divider()
    keyframe_time = st.number_input(
        "Cast-frame timestamp (seconds)",
        min_value=0.0,
        max_value=30.0,
        value=1.5,
        step=0.25,
        help="Choose a moment where both source performers are clearly visible and not occluding each other.",
    )
    restore_audio = st.toggle("Restore original source audio", value=True)
    trim_transition = st.toggle(
        "Remove DreamActor's 1-second intro transition",
        value=True,
        help="DreamActor documents a 1-second transition at the start. When enabled, the app also shifts the source audio by 1 second when remuxing.",
    )
    st.caption("Recommended: source clip ≤30 seconds, clear frontal or 3/4 views, one clean reference image per replacement person.")

col_main, col_side = st.columns([1.18, 0.82], gap="large")

with col_main:
    st.markdown('<span class="step">Step 1</span>', unsafe_allow_html=True)
    st.subheader("Source performance")
    source_video = st.file_uploader("Upload source video", type=["mp4", "mov", "webm"], key="source_video")
    if source_video:
        st.video(source_video)

    st.markdown('<span class="step">Step 2</span>', unsafe_allow_html=True)
    st.subheader("Replacement people")
    c1, c2 = st.columns(2)
    with c1:
        left_name = st.text_input("LEFT performer label", value="Person A")
        left_ref = st.file_uploader(
            "LEFT reference image",
            type=["jpg", "jpeg", "png", "webp"],
            key="left_ref",
            help="Use one clear photo showing the face and, ideally, the desired visible outfit.",
        )
        left_notes = st.text_area(
            "LEFT instructions",
            placeholder="e.g. Keep the black jacket, short hair and white sneakers from the reference.",
            height=100,
        )
        if left_ref:
            st.image(left_ref, caption="LEFT reference", use_container_width=True)

    with c2:
        right_name = st.text_input("RIGHT performer label", value="Person B")
        right_ref = st.file_uploader(
            "RIGHT reference image",
            type=["jpg", "jpeg", "png", "webp"],
            key="right_ref",
            help="Use one clear photo showing the face and, ideally, the desired visible outfit.",
        )
        right_notes = st.text_area(
            "RIGHT instructions",
            placeholder="e.g. Keep gray hair, pale polo, gray shorts and sandals.",
            height=100,
        )
        if right_ref:
            st.image(right_ref, caption="RIGHT reference", use_container_width=True)

    st.markdown('<span class="step">Step 3</span>', unsafe_allow_html=True)
    st.subheader("Build the cast frame")
    cast_extra = st.text_area(
        "Cast-frame instructions",
        value="Preserve the source frame's original orange studio, hanging microphone and framing. Keep facial expressions close to the source frame while changing identity and visible wardrobe.",
        height=115,
    )

    # Local keyframe preview, before spending fal credits.
    if source_video:
        try:
            with tempfile.TemporaryDirectory(prefix="preview_") as td:
                td = Path(td)
                source_preview_path = save_upload(source_video, td, "source")
                duration = video_duration_seconds(source_preview_path)
                if duration is not None:
                    st.caption(f"Detected source duration: {duration:.2f}s")
                    if duration > 30.2:
                        st.error("DreamActor supports source videos up to 30 seconds. Trim this clip before generating.")
                    if keyframe_time >= duration:
                        st.warning("The selected cast-frame timestamp is beyond the end of the source clip.")
                frame_path = td / "cast_preview.png"
                if duration is None or keyframe_time < duration:
                    extract_frame(source_preview_path, keyframe_time, frame_path)
                    st.image(frame_path.read_bytes(), caption=f"Source frame at {keyframe_time:.2f}s", use_container_width=True)
        except Exception as exc:
            st.warning(f"Could not preview the selected frame: {exc}")

    ready_for_cast = bool(source_video and left_ref and right_ref)
    build_cast = st.button(
        "1 · Generate replacement cast frame",
        type="primary",
        use_container_width=True,
        disabled=not ready_for_cast,
    )

with col_side:
    st.markdown('<span class="step">Step 4</span>', unsafe_allow_html=True)
    st.subheader("Animate the cast frame")
    st.write(
        "This version does **not** send your real-person references through Seedance 2.5. "
        "It first creates one edited still, then uses DreamActor V2 to transfer the source video's motion, facial expressions and lip movement to that still."
    )

    if st.session_state.cast_frame_url:
        st.image(st.session_state.cast_frame_url, caption="Generated cast frame", use_container_width=True)
        st.success("Cast frame ready. If the left/right identities look correct, animate it below.")
    else:
        st.info("Generate the cast frame first. This lets you inspect identity placement before paying for the full video render.")

    direct_cast = st.file_uploader(
        "Optional: upload your own finished two-person cast frame",
        type=["jpg", "jpeg", "png", "webp"],
        help="If you already made a still where the two desired people occupy the source performers' positions, upload it here and skip the cast-frame generation step.",
        key="direct_cast",
    )
    if direct_cast:
        st.image(direct_cast, caption="Uploaded cast frame", use_container_width=True)

    can_animate = bool(source_video and (direct_cast or st.session_state.cast_frame_url))
    animate = st.button(
        "2 · Animate with source performance",
        type="primary",
        use_container_width=True,
        disabled=not can_animate,
    )

    if st.session_state.final_bytes:
        st.divider()
        st.subheader("Result")
        st.video(st.session_state.final_bytes)
        st.download_button(
            "Download final MP4",
            data=st.session_state.final_bytes,
            file_name=st.session_state.final_name,
            mime="video/mp4",
            use_container_width=True,
        )


# ---------- Stage 1: build cast frame ----------
if build_cast:
    if api_key.strip():
        os.environ["FAL_KEY"] = api_key.strip()
    elif not os.getenv("FAL_KEY"):
        st.error("Add FAL_KEY in Streamlit Secrets or enter a fal API key in the sidebar.")
        st.stop()

    current_signature = fingerprint(source_video, left_ref, right_ref, keyframe_time, left_name, right_name, left_notes, right_notes, cast_extra)
    with tempfile.TemporaryDirectory(prefix="cast_frame_") as td:
        td = Path(td)
        source_path = save_upload(source_video, td, "source")
        left_path = save_upload(left_ref, td, "left")
        right_path = save_upload(right_ref, td, "right")

        duration = video_duration_seconds(source_path)
        if duration is not None and duration > 30.2:
            st.error("Source video is over 30 seconds. Trim it and upload the shorter clip.")
            st.stop()
        if duration is not None and keyframe_time >= duration:
            st.error("Cast-frame timestamp is beyond the end of the source video.")
            st.stop()

        frame_path = td / "base_frame.png"
        extract_frame(source_path, keyframe_time, frame_path)
        cast_prompt = build_cast_prompt(left_name, right_name, left_notes, right_notes, cast_extra)

        with st.status("Building replacement cast frame…", expanded=True) as status:
            try:
                base_url = upload_to_fal(frame_path)
                status.write("Source keyframe uploaded")
                left_url = upload_to_fal(left_path)
                status.write("LEFT reference uploaded")
                right_url = upload_to_fal(right_path)
                status.write("RIGHT reference uploaded")

                # Cache source upload for Stage 2 if the same source remains selected.
                source_url = upload_to_fal(source_path)
                st.session_state.source_fal_url = source_url
                st.session_state.source_signature = fingerprint(source_video)
                status.write("Source video uploaded")

                hy_result = subscribe_with_status(
                    HY_WU_MODEL,
                    {
                        "prompt": cast_prompt,
                        "image_urls": [base_url, left_url, right_url],
                    },
                    status,
                    "Submitting face / wardrobe cast-frame edit…",
                )
                images = hy_result.get("images", []) if isinstance(hy_result, dict) else []
                cast_url = images[0].get("url") if images else None
                if not cast_url:
                    raise RuntimeError("HY-WU returned no output image URL.")

                st.session_state.cast_frame_url = cast_url
                st.session_state.cast_signature = current_signature
                st.session_state.final_bytes = None
                status.update(label="Cast frame ready", state="complete", expanded=False)
            except Exception as exc:
                status.update(label="Cast-frame generation failed", state="error", expanded=True)
                st.error(friendly_error(exc))
                with st.expander("Technical details"):
                    st.code(str(exc))
                st.stop()

    st.rerun()


# ---------- Stage 2: animate ----------
if animate:
    if api_key.strip():
        os.environ["FAL_KEY"] = api_key.strip()
    elif not os.getenv("FAL_KEY"):
        st.error("Add FAL_KEY in Streamlit Secrets or enter a fal API key in the sidebar.")
        st.stop()

    with tempfile.TemporaryDirectory(prefix="animate_") as td:
        td = Path(td)
        source_path = save_upload(source_video, td, "source")
        duration = video_duration_seconds(source_path)
        if duration is not None and duration > 30.2:
            st.error("Source video is over 30 seconds. Trim it and upload the shorter clip.")
            st.stop()

        with st.status("Preparing animation…", expanded=True) as status:
            try:
                # Source video URL: reuse Stage 1 upload if it matches the selected source.
                source_sig = fingerprint(source_video)
                if st.session_state.source_fal_url and st.session_state.source_signature == source_sig:
                    source_url = st.session_state.source_fal_url
                    status.write("Reusing uploaded source video")
                else:
                    source_url = upload_to_fal(source_path)
                    st.session_state.source_fal_url = source_url
                    st.session_state.source_signature = source_sig
                    status.write("Source video uploaded")

                # Cast frame: direct upload takes precedence; otherwise use generated frame.
                if direct_cast:
                    cast_in = save_upload(direct_cast, td, "direct_cast")
                else:
                    downloaded = td / "generated_cast_input"
                    download_file(st.session_state.cast_frame_url, downloaded)
                    cast_in = downloaded

                normalized_cast = td / "cast_for_dreamactor.jpg"
                normalize_dreamactor_image(cast_in, normalized_cast)
                cast_url = upload_to_fal(normalized_cast)
                status.write("Cast frame prepared for DreamActor")

                dream_result = subscribe_with_status(
                    DREAMACTOR_MODEL,
                    {
                        "image_url": cast_url,
                        "video_url": source_url,
                        "trim_first_second": bool(trim_transition),
                    },
                    status,
                    "Transferring source motion with DreamActor V2…",
                )
                video = dream_result.get("video", {}) if isinstance(dream_result, dict) else {}
                output_url = video.get("url")
                if not output_url:
                    raise RuntimeError("DreamActor returned no video URL.")

                generated_path = td / "generated.mp4"
                download_file(output_url, generated_path)
                final_path = generated_path

                if restore_audio:
                    remuxed = td / "final_with_original_audio.mp4"
                    audio_offset = 1.0 if trim_transition else 0.0
                    remux_original_audio(
                        generated_path,
                        source_path,
                        remuxed,
                        source_audio_offset_s=audio_offset,
                    )
                    final_path = remuxed
                    status.write("Original source audio restored")

                st.session_state.final_bytes = final_path.read_bytes()
                st.session_state.final_name = "hotel_lobby_character_swap.mp4"
                status.update(label="Video complete", state="complete", expanded=False)
            except Exception as exc:
                status.update(label="Animation failed", state="error", expanded=True)
                st.error(friendly_error(exc))
                with st.expander("Technical details"):
                    st.code(str(exc))
                st.stop()

    st.rerun()


st.divider()
st.caption(
    "Use reference material you have permission to use. This workflow uses fal's HY-WU image editor for the cast frame and DreamActor V2 for motion transfer; provider safety rules still apply."
)
