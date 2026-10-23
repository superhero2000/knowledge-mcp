// Render player.html frame by frame (deterministic time) and pipe PNGs into ffmpeg.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const FPS = 30;
const dir = process.env.VIDEO_WORKDIR || "/tmp/rag-demo-video";
const out = process.argv[2] || path.join(dir, "knowledge-mcp-demo.mp4");
const previewOnly = process.argv.includes("--preview");

(async () => {
  const scenes = fs.readFileSync(path.join(dir, "scenes.json"), "utf8");
  const html = fs.readFileSync(path.join(__dirname, "player.html"), "utf8").replace("__SCENES__", scenes);
  fs.writeFileSync(path.join(dir, "player.built.html"), html);

  const browser = await chromium.launch(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {});
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  await page.goto("file://" + path.join(dir, "player.built.html"));
  await page.evaluate(() => document.fonts.ready);
  const total = await page.evaluate(() => window.TOTAL);
  console.log(`duration ${total.toFixed(1)}s`);

  if (previewOnly) {
    // a few stills for checking the layout
    const times = await page.evaluate(() => (window.__t = null, null));
    const starts = await page.evaluate(() => SCENES.map(s => s.start + s.dur - 0.5));
    for (const [i, t] of starts.entries()) {
      await page.evaluate(t => window.renderAt(t), t);
      await page.screenshot({ path: path.join(dir, `preview-${String(i).padStart(2, "0")}.png`) });
    }
    await browser.close();
    return;
  }

  const ff = spawn("ffmpeg", ["-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", String(FPS), "-i", "-",
    "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out],
    { stdio: ["pipe", "inherit", "inherit"] });

  const frames = Math.ceil(total * FPS);
  let buf = null, lastKey = null, shots = 0;
  for (let f = 0; f < frames; f++) {
    const key = await page.evaluate(t => window.renderAt(t), f / FPS);
    if (key !== lastKey || !buf) {
      buf = await page.screenshot({ type: "png" });
      lastKey = key;
      shots++;
    }
    if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once("drain", r));
    if (f % (FPS * 20) === 0) console.log(`frame ${f}/${frames} (${shots} unique)`);
  }
  ff.stdin.end();
  await new Promise(r => ff.on("close", r));
  await browser.close();
  console.log(`wrote ${out}: ${frames} frames, ${shots} rendered`);
})();
