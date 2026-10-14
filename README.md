# Pulse-3
Meh 😑
Fintech Pulse Studio
Streamlit port of the Fintech Pulse clip desk. Topic -> script -> voice -> 1080x1920 short (MP4), with SRT captions, voice MP3 and script JSON. Free by default: no API keys.
Deploy on Streamlit Community Cloud
Upload these files to the ROOT of a GitHub repo: app.py, requirements.txt, packages.txt, .streamlit/config.toml.
share.streamlit.io -> New app -> pick the repo, branch main, main file app.py.
Optional (Settings -> Secrets): XAI_API_KEY = "..." to use Grok for scripts.
Notes
Voice: Edge-TTS (free) with gTTS fallback. Captions follow the spoken words when Edge-TTS returns word timestamps.
Backgrounds: circuit plate (house look), trading-chart motion, aurora glow, or your own clips/photos.
"AI script (free, best effort)" calls a public keyless endpoint and falls back to the template if it is unavailable.
Drafts are not financial advice. Check claims, affiliate disclosures and platform rules before posting.