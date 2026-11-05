#!/usr/bin/env node
/**
 * 可选自检：用 Playwright/Chromium 打开生成的贴片定位图，验证渲染与交互。
 *
 *   node verify.mjs <板名_贴片定位图.html> [截图目录]
 *
 * 环境变量：
 *   PLAYWRIGHT_MODULE_DIR  装有 node_modules/playwright 的目录（CWD 里没装时指定）
 *   PLAYWRIGHT_BROWSERS_PATH  浏览器二进制目录（~/.cache 只读时需要）
 *
 * 退出码：0 全部通过；1 有断言/页面错误；2 缺少 playwright。
 */
import path from "node:path";
import fs from "node:fs";
import { pathToFileURL } from "node:url";

const args = process.argv.slice(2);
const htmlArg = args[0];
const outDir = path.resolve(args[1] || "verify_shots");
if (!htmlArg) {
  console.error("用法: node verify.mjs <xxx_贴片定位图.html> [截图目录]");
  process.exit(2);
}
const htmlPath = path.resolve(htmlArg);
if (!fs.existsSync(htmlPath)) {
  console.error("找不到文件:", htmlPath);
  process.exit(2);
}

async function loadPlaywright() {
  const candidates = [];
  if (process.env.PLAYWRIGHT_MODULE_DIR) {
    const modDir = path.join(process.env.PLAYWRIGHT_MODULE_DIR, "node_modules", "playwright");
    candidates.push(pathToFileURL(path.join(modDir, "index.mjs")).href);
    candidates.push(pathToFileURL(path.join(modDir, "index.js")).href);
  }
  candidates.push("playwright");
  for (const c of candidates) {
    try {
      const m = await import(c);
      if (m.chromium) return m;
      if (m.default && m.default.chromium) return m.default;   // CJS interop
    } catch { /* try next */ }
  }
  console.error(`
[!] 没有找到 playwright。安装方法：
      npm init -y && npm i playwright && npx playwright install chromium
    ~/.npm 或 ~/.cache 只读时（沙箱里常见）把缓存和浏览器目录指到工作目录：
      npm i playwright --cache "$PWD/.npm-cache"
      PLAYWRIGHT_BROWSERS_PATH="$PWD/.pw-browsers" npx playwright install chromium
    然后重跑：
      PLAYWRIGHT_BROWSERS_PATH="$PWD/.pw-browsers" node verify.mjs <html> [shots]
    （如果 playwright 装在别的目录，用 PLAYWRIGHT_MODULE_DIR=<那个目录>）
`);
  process.exit(2);
}

const { chromium } = await loadPlaywright();
fs.mkdirSync(outDir, { recursive: true });

const errors = [];
const fails = [];
const ok = (cond, msg) => { if (!cond) fails.push(msg); return cond; };
const log = (...a) => console.log(...a);

const browser = await chromium.launch({ args: ["--no-sandbox", "--disable-dev-shm-usage"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
page.on("console", (m) => { if (m.type() === "error") errors.push("[console] " + m.text()); });
page.on("pageerror", (e) => errors.push("[pageerror] " + e.message));
page.on("requestfailed", (r) => errors.push("[requestfailed] " + r.url()));

const shot = async (name) => {
  const p = path.join(outDir, name);
  await page.screenshot({ path: p });
  log("  截图:", p);
};

try {
  await page.goto(pathToFileURL(htmlPath).href);
  await page.waitForFunction("window.__SMT_READY === true", { timeout: 30000 });
  log("[1/6] 页面加载成功 (__SMT_READY)");
  await page.waitForTimeout(400);
  await shot("01_top.png");

  // 数据 vs 界面
  const info = await page.evaluate(() => {
    const S = window.__SMT.state;
    return {
      title: document.title,
      h1: document.querySelector(".brand h1").textContent,
      comps: window.__SMT.comps.length,
      navComps: parseInt(document.querySelector("#cnt-all").textContent, 10),
      top: parseInt(document.querySelector("#cnt-top").textContent, 10),
      bot: parseInt(document.querySelector("#cnt-bot").textContent, 10),
      rows: document.querySelectorAll(".row").length,
      dims: document.querySelector(".dims") ? document.querySelector(".dims").textContent : "",
      side: S.side, z: Math.round(S.z),
    };
  });
  log("[2/6] 数据:", JSON.stringify(info));
  const boardOnly = info.comps === 0;
  if (boardOnly) {
    log("      这个文件只有板图、没有元件（例如没有坐标文件的 Gerber）——跳过位号相关检查");
  } else {
    ok(info.navComps === info.comps, `页头元件数(${info.navComps}) 与数据(${info.comps}) 不一致`);
    ok(info.top + info.bot === info.comps, "顶层+底层元件数 不等于 总数");
    ok(info.rows > 0, "元件列表为空");
  }
  ok(/mm/.test(info.dims), "板尺寸没有显示");

  // 底层切换
  await page.click('.sideseg button[data-side="bottom"]');
  await page.waitForTimeout(500);
  const bottom = await page.evaluate(() => ({
    side: window.__SMT.state.side,
    rows: document.querySelectorAll(".row").length,
  }));
  log("[3/6] 切到底层:", JSON.stringify(bottom));
  ok(bottom.side === "bottom", "切换到顶层/底层失败");
  await shot("02_bottom.png");

  if (!boardOnly) {
    // 搜索并定位（用数据里第 1 个元件位号）
    const des = await page.evaluate(() => window.__SMT.comps[0].d);
    await page.fill("#search", des);
    await page.press("#search", "Enter");
    await page.waitForTimeout(700);
    const sel = await page.evaluate(() => {
      const S = window.__SMT.state;
      return {
        des: S.sel && S.sel.d, z: Math.round(S.z),
        card: !document.querySelector("#detail").classList.contains("hide"),
        cardText: document.querySelector("#detail").innerText.replace(/\s+/g, " ").slice(0, 80),
      };
    });
    log("[4/6] 搜索定位:", des, "->", JSON.stringify(sel));
    ok(sel.des === des, `搜索 ${des} 没有选中元件`);
    ok(sel.card, "元件信息卡没有出现");
    await shot("03_search.png");
    // 选中后至少不能变小（小板/大元件时 fit 视图本身就已经够近）
    ok(sel.z >= info.z * 0.999, `选中元件后反而缩小了 (${info.z} -> ${sel.z})`);
    await shot("04_zoom.png");
  } else {
    log("[4/6] 跳过搜索定位（无元件）");
  }

  const errsNow = errors.length;
  log(`[5/6] 页面错误: ${errsNow}`);
  ok(errsNow === 0, `${errsNow} 个 console/page 错误：${errors.slice(0, 3).join(" | ")}`);

  log("[6/6] 完成");
} catch (err) {
  fails.push("运行时异常: " + err.message);
} finally {
  await browser.close();
}

if (fails.length) {
  console.error("\n❌ 失败 " + fails.length + " 项：");
  for (const f of fails) console.error("   - " + f);
  if (errors.length) {
    console.error("   页面错误：");
    for (const e of errors.slice(0, 10)) console.error("     " + e);
  }
  process.exit(1);
}
console.log("\n✅ 全部通过（截图在 " + outDir + " ，用 read_image 看一眼版式是否正常）");
