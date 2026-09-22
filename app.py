import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests
import streamlit as st
import fal_client


st.set_page_config(
    page_title="fal AI Character Edit Studio",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .stApp { background: #0d0d10; }
    .block-container { max-width: 1200px; padding-top: 2rem; padding-bottom: 4rem; }
    .hero {
        padding: 1.4rem 1.5rem;
        border: 1px solid rgba(255,255,255,.09);
        border-radius: 18px;
        background: linear-gradient(145deg, rgba(113,75,255,.16), rgba(255,255,255,.025));
        margin-bottom: 1.2rem;
    }
    .hero h1 { margin: 0 0 .25rem 0; font-size: 2rem; }
    .hero p { margin: 0; opacity: .72; }
    .muted { opacity: .65; font-size: .9rem; }
    .step {
        display: inline-block;
        font-size: .78rem;
        font-weight: 700;
        letter-spacing: .05em;
        text-transform: uppercase;
        opacity: .7;
        margin-bottom: .35rem;
    }
    div[data-testid="stFileUploader"] section {
        border-radius: 14px;
        border-color: rgba(255,255,255,.14);
        background: rgba(255,255,255,.02);
    }
    div[data-testid="stAlert"] { border-radius: 12px; }
    .stButton > button { border-radius: 12px; font-weight: 700; }
    .stDownloadButton > button { border-radius: 12px; font-weight: 700; }
    </style>
    """,
    unsafe_allow_html=True,
)

MODEL_GLOBAL = "bytedance/seedance-2.5/reference-to-video"
MODEL_US = "bytedance/seedance-2.5/us/reference-to-video"


def save_upload(uploaded_file, folder: Path) -> Path:
    suffix = Path(uploaded_file.name).suffix or ".bin"
    safe_name = f"upload_{abs(hash(uploaded_file.name))}{suffix}"
    path = folder / safe_name
    path.write_bytes(uploaded_file.getbuffer())
    return path


def upload_to_fal(path: Path) -> str:
    return fal_client.upload_file(str(path))


def build_prompt(left_name, right_name, left_notes, right_notes, left_count, right_count, extra):
    left_refs = [f"@Image{i}" for i in range(1, left_count + 1)]
    right_start = left_count + 1
    right_refs = [f"@Image{i}" for i in range(right_start, right_start + right_count)]

    left_ref_text = ", ".join(left_refs) if left_refs else "the LEFT reference images"
    right_ref_text = ", ".join(right_refs) if right_refs else "the RIGHT reference images"

    prompt = f"""Edit the entire source video @Video1.

Replace the viewer's LEFT performer with {left_name}.
Use {left_ref_text} for the LEFT performer's identity, face, hair, build, and wardrobe. {left_notes.strip() or 'Keep the subject visually consistent across the full clip.'}

Replace the viewer's RIGHT performer with {right_name}.
Use {right_ref_text} for the RIGHT performer's identity, face, hair, build, and wardrobe. {right_notes.strip() or 'Keep the subject visually consistent across the full clip.'}

The reference images provide identity and wardrobe only. The source video provides motion, expressions, gestures, choreography, timing, interactions, camera cuts, framing, lighting, microphone placement, and background.
Preserve the original performance as faithfully as possible. Do not swap positions. Do not invent new movement, camera motion, props, or scene elements. Maintain stable faces and clothing across every frame and camera cut.
"""
    if extra.strip():
        prompt += "\nAdditional instructions:\n" + extra.strip() + "\n"
    return prompt.strip()


def download_file(url: str, out_path: Path):
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with out_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def remux_original_audio(generated_path: Path, source_path: Path, output_path: Path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg = None
    if not ffmpeg:
        raise RuntimeError("FFmpeg was not found. Install ffmpeg or imageio-ffmpeg.")

    cmd = [
        ffmpeg,
        "-y",
        "-i", str(generated_path),
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
        raise RuntimeError(proc.stderr[-4000:])


def call_fal(model, prompt, video_url, image_urls, resolution, preserve_audio, bitrate_mode, end_user_id, status_box):
    args = {
        "prompt": prompt,
        "task": "editing",
        "image_urls": image_urls,
        "video_urls": [video_url],
        "resolution": resolution,
        "duration": "auto",
        "aspect_ratio": "auto",
        "generate_audio": not preserve_audio,
        "bitrate_mode": bitrate_mode,
    }
    if end_user_id.strip():
        args["end_user_id"] = end_user_id.strip()

    def on_queue_update(update):
        try:
            if isinstance(update, fal_client.InProgress):
                logs = getattr(update, "logs", None) or []
                if logs:
                    msg = logs[-1].get("message", "Rendering…")
                    status_box.write(msg)
        except Exception:
            pass

    result = fal_client.subscribe(
        model,
        arguments=args,
        with_logs=True,
        on_queue_update=on_queue_update,
    )
    return result, args


st.markdown(
    """
    <div class="hero">
      <h1>fal AI Character Edit Studio</h1>
      <p>Upload a performance video + reference photos, replace the left/right performers, and optionally restore the source audio exactly.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Configuration")
    env_key = os.getenv("FAL_KEY", "")
    api_key = st.text_input(
        "fal API key",
        value="" if not env_key else "",
        type="password",
        placeholder="Uses FAL_KEY from env if left blank",
        help="For local use only. The key is kept in this Streamlit process and is never written to disk by this app.",
    )
    if env_key:
        st.success("FAL_KEY detected in environment")
    else:
        st.caption("Tip: create a .env / shell variable named FAL_KEY instead of pasting the key each time.")

    use_us = st.toggle("Use US-hosted Seedance endpoint", value=False)
    model = MODEL_US if use_us else MODEL_GLOBAL
    st.code(model, language=None)

    resolution = st.selectbox("Resolution", ["480p", "720p"], index=1)
    bitrate_mode = st.selectbox("Bitrate", ["standard", "high"], index=0)
    preserve_audio = st.toggle("Restore original source audio", value=True, help="Runs Seedance without generated audio, then remuxes the original audio back onto the result.")
    end_user_id = st.text_input("end_user_id (optional)", placeholder="e.g. internal-user-001")

    st.divider()
    st.caption("Seedance 2.5 editing expects a reference video roughly 1.8–30.2 seconds long. Short, tightly cropped clips usually work best.")

left, right = st.columns([1.15, 0.85], gap="large")

with left:
    st.markdown('<span class="step">Step 1</span>', unsafe_allow_html=True)
    st.subheader("Source performance")
    source_video = st.file_uploader("Upload source video", type=["mp4", "mov"], accept_multiple_files=False)
    if source_video:
        st.video(source_video)

    st.markdown('<span class="step">Step 2</span>', unsafe_allow_html=True)
    st.subheader("Reference people")
    c1, c2 = st.columns(2)
    with c1:
        left_name = st.text_input("LEFT performer name / label", value="Person A")
        left_refs = st.file_uploader(
            "LEFT reference images",
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=True,
            key="left_refs",
            help="Use 1–3 clean face/full-body photos. Order matters: these become @Image1, @Image2, …",
        )
        left_notes = st.text_area(
            "LEFT appearance instructions",
            placeholder="e.g. Use the first image for outfit, second for build, third for facial detail. Keep black jacket and white sneakers.",
            height=105,
        )
    with c2:
        right_name = st.text_input("RIGHT performer name / label", value="Person B")
        right_refs = st.file_uploader(
            "RIGHT reference images",
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=True,
            key="right_refs",
            help="These are numbered after the LEFT references.",
        )
        right_notes = st.text_area(
            "RIGHT appearance instructions",
            placeholder="e.g. Use the last image for wardrobe. Keep gray hair and brown sandals. Ignore other people in group photos.",
            height=105,
        )

    if left_refs or right_refs:
        st.caption("Reference numbering")
        mapping = []
        idx = 1
        for f in left_refs or []:
            mapping.append({"ref": f"@Image{idx}", "side": "LEFT", "file": f.name})
            idx += 1
        for f in right_refs or []:
            mapping.append({"ref": f"@Image{idx}", "side": "RIGHT", "file": f.name})
            idx += 1
        st.dataframe(mapping, hide_index=True, use_container_width=True)

with right:
    st.markdown('<span class="step">Step 3</span>', unsafe_allow_html=True)
    st.subheader("Edit instructions")
    extra = st.text_area(
        "Additional instructions",
        value="Keep the original background, framing, camera cuts, body motion, and choreography. Keep the hanging microphone and studio lighting. Remove unintended accessories unless they are part of the references.",
        height=150,
    )

    prompt = build_prompt(
        left_name,
        right_name,
        left_notes,
        right_notes,
        len(left_refs or []),
        len(right_refs or []),
        extra,
    )
    prompt_override = st.text_area("Prompt sent to fal", value=prompt, height=390)

    st.markdown('<span class="step">Step 4</span>', unsafe_allow_html=True)
    ready = bool(source_video and left_refs and right_refs)
    if not ready:
        st.info("Add a source video and at least one reference image for each performer.")

    generate = st.button("Generate character edit", type="primary", use_container_width=True, disabled=not ready)

if generate:
    if api_key.strip():
        os.environ["FAL_KEY"] = api_key.strip()
    elif not os.getenv("FAL_KEY"):
        st.error("Add your fal API key in the sidebar or set FAL_KEY in your environment.")
        st.stop()

    with tempfile.TemporaryDirectory(prefix="fal_edit_") as tmp:
        tmpdir = Path(tmp)
        source_path = save_upload(source_video, tmpdir)
        ref_paths = [save_upload(f, tmpdir) for f in (left_refs or []) + (right_refs or [])]

        with st.status("Uploading source + references to fal…", expanded=True) as status:
            try:
                video_url = upload_to_fal(source_path)
                status.write("Source video uploaded")
                image_urls = []
                for i, p in enumerate(ref_paths, start=1):
                    image_urls.append(upload_to_fal(p))
                    status.write(f"Reference {i}/{len(ref_paths)} uploaded")

                status.write("Submitting Seedance 2.5 editing job…")
                result, request_args = call_fal(
                    model=model,
                    prompt=prompt_override,
                    video_url=video_url,
                    image_urls=image_urls,
                    resolution=resolution,
                    preserve_audio=preserve_audio,
                    bitrate_mode=bitrate_mode,
                    end_user_id=end_user_id,
                    status_box=status,
                )
                status.update(label="Generation complete", state="complete", expanded=False)
            except Exception as e:
                status.update(label="Generation failed", state="error", expanded=True)
                st.exception(e)
                st.stop()

        video = result.get("video", {}) if isinstance(result, dict) else {}
        output_url = video.get("url")
        if not output_url:
            st.error("fal returned no video URL.")
            st.json(result)
            st.stop()

        generated_path = tmpdir / "generated.mp4"
        download_file(output_url, generated_path)
        final_path = generated_path

        if preserve_audio:
            remuxed = tmpdir / "final_with_original_audio.mp4"
            try:
                remux_original_audio(generated_path, source_path, remuxed)
                final_path = remuxed
                st.success("Original source audio restored onto the generated video.")
            except Exception as e:
                st.warning(f"Video generated, but original-audio remux failed: {e}")

        final_bytes = final_path.read_bytes()
        st.subheader("Result")
        st.video(final_bytes)
        st.download_button(
            "Download final MP4",
            data=final_bytes,
            file_name="fal_character_edit.mp4",
            mime="video/mp4",
            use_container_width=True,
        )

        with st.expander("API request / response"):
            st.code(json.dumps({"model": model, "input": request_args}, indent=2), language="json")
            st.code(json.dumps(result, indent=2), language="json")

st.divider()
st.caption("Use reference material you have permission to use. For best consistency, keep the source clip short and use clear, single-subject face/full-body references.")
