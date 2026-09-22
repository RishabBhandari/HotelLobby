# fal AI Character Edit Studio

A small Streamlit app for reference-guided video editing with **fal AI / Seedance 2.5**.

It is designed for the workflow where you:

1. Upload a short source performance video.
2. Upload reference photos for the performer on the viewer's **left** and **right**.
3. Give identity / outfit instructions.
4. Send the job to `bytedance/seedance-2.5/reference-to-video` with `task="editing"`.
5. Optionally restore the **original source audio** onto the generated video.

## Setup

Python 3.10+ is recommended.

```bash
python -m venv .venv
```

Activate the venv:

**macOS / Linux**
```bash
source .venv/bin/activate
```

**Windows PowerShell**
```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Set your fal key:

**macOS / Linux**
```bash
export FAL_KEY="YOUR_KEY"
```

**Windows PowerShell**
```powershell
$env:FAL_KEY="YOUR_KEY"
```

Then run:

```bash
streamlit run app.py
```

The app will open in your browser, usually at `http://localhost:8501`.

## Notes

- The app does **not** hard-code your API key.
- You can also paste a key into the sidebar for the current Streamlit process, but an environment variable is safer.
- Seedance 2.5 editing is best with a short, tightly framed source clip and clear reference photos.
- Reference images are numbered in upload order as `@Image1`, `@Image2`, etc. The source clip is `@Video1`.
- If **Restore original source audio** is enabled, the app asks Seedance not to generate audio and then remuxes the source audio onto the result using FFmpeg (via `imageio-ffmpeg` if system FFmpeg is unavailable).
- Generation can be expensive; 480p is useful for iteration, then move to 720p for the final run.

## Current fal endpoint

Default:

```text
bytedance/seedance-2.5/reference-to-video
```

Optional US-hosted endpoint:

```text
bytedance/seedance-2.5/us/reference-to-video
```

The app sends `task="editing"`, which is the mode intended to modify a supplied reference video while retaining its structure.
