# Demo video

`../knowledge-mcp-demo.mp4` is generated from a real session: every command runs live
against the configured provider on a fresh copy of the project, then the recorded output is
rendered as an animated terminal (1920x1080, 30 fps).

```bash
export OPENAI_API_KEY=sk-...                  # or a proxy-injected credential
python docs/video/capture.py                   # runs the demo, writes $VIDEO_WORKDIR/scenes.json
npm i playwright                               # once; needs ffmpeg on PATH
node docs/video/record.js docs/knowledge-mcp-demo.mp4
```

`VIDEO_WORKDIR` defaults to `/tmp/rag-demo-video`. Add `--preview` to `record.js` to write one PNG per scene instead.

The current video was recorded before the project was renamed, so it still shows the old name `local-rag-mcp`.
