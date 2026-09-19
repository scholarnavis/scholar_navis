/**
 * Scholar Navis 发布站点：scholarnavis.com 的 Cloudflare Pages / Worker 入口。
 *
 * 负责什么
 * --------
 * 1. 列举 R2 上的构建产物，向应用提供**两条通道**（stable / dev）的最新版本号；
 * 2. 提供指定通道的产物下载（浏览器直下 zip）；
 * 3. 代理 GitHub Release 的更新日志，让应用只与 scholarnavis.com 通信
 *    （应用内查看 Markdown，不必直连 api.github.com）。
 *
 * 命名与通道契约（与仓库侧必须一致）
 * ----------------------------------
 * 产物对象名 = `scholar_navis_{平台}_{通道}_v{版本}.zip`，例如
 *   scholar_navis_win_stable_v2.0.6.zip
 *   scholar_navis_linux_dev_v2.0.7-dev-1.zip
 * 通道判定（版本号里含 `-dev` 即 dev）由 `src/core/version.py::release_channel`
 * 决定，本文件只做**字符串前缀匹配**，不重新判定通道，避免两边漂移。
 *
 * 路由
 * ----
 *   GET /versions?os=linux              → JSON：该平台两条通道的最新版本
 *   GET /latest?os=win&channel=dev      → 纯文本：指定通道的最新版本（旧客户端兼容）
 *   GET /dl?os=linux&channel=dev        → zip 附件下载
 *   GET /changelog?version=2.0.7-dev-1  → JSON：GitHub Release 的 Markdown 正文
 *   GET /ico.svg | /favicon.ico         → R2 上的站点图标
 *   GET /                               → 301 到官网首页
 * 不支持的平台（macOS）：/dl 返回"敬请期待"页面；/versions、/latest 仍返回
 * `0.0.0`，避免应用把 HTML 当成版本号解析。
 *
 * 需要的绑定与变量（Cloudflare 控制台）
 * ------------------------------------
 *   R2 bucket binding : scholarnavis
 *   可选变量(明文)     : SCHOLAR_NAVIS_REPO = scholarnavis/scholar_navis
 *   可选密钥(加密)     : GITHUB_TOKEN —— 未认证调用 GitHub API 的配额是
 *                        "每出口 IP 每小时 60 次"，而 Cloudflare 出口 IP 是共享的，
 *                        生产环境应配置该 token（5000 次/小时）。更新日志有边缘缓存
 *                        兜底，不配也能跑，但冷启动高峰期可能取不到日志。
 */

const REPO = "scholarnavis/scholar_navis";
const OBJECT_STEM = "scholar_navis";
const HOME_URL = "https://scholarnavis.com";
const CHANNELS = ["stable", "dev"];
const NOT_AVAILABLE = "0.0.0";
/** 支持的平台（macOS 暂无独立二进制，见 unsupportedOsHTML）。 */
const SUPPORTED_TAGS = ["win", "linux"];
/** 更新日志的边缘缓存时长（秒）。 */
const CHANGELOG_TTL = 21600;

/** os 查询值 → 产物文件名里的平台标记（应用侧传 platform.system().lower()）。 */
const OS_TAGS = {
  windows: "win",
  win: "win",
  linux: "linux",
  darwin: "mac",
  mac: "mac",
  macos: "mac",
};

/** 产物名解析：scholar_navis_{平台}_{通道}_v{版本}.zip */
const ASSET_RE = /^scholar_navis_([a-z]+)_([a-z]+)_v(.+)\.zip$/;

/** 版本阶段权重：dev/alpha < beta < rc < final，与仓库侧保持一致。 */
const STAGE_WEIGHTS = { dev: 1, alpha: 1, beta: 2, rc: 3, final: 4 };

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
  "Access-Control-Allow-Headers": "*",
};

const unsupportedOsHTML = `<!DOCTYPE html>
<html lang="en" class="scroll-smooth">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>macOS Status | Scholar Navis</title>

    <link rel="icon" type="image/svg+xml" href="/ico.svg">

    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    colors: { primary: '#3b82f6', darkBg: '#0f172a', darkSurface: '#1e293b' }
                }
            }
        }
        function setTheme(theme) {
            if (theme === 'dark' || (theme === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
                document.documentElement.classList.add('dark');
            } else {
                document.documentElement.classList.remove('dark');
            }
        }
        setTheme('auto');
    </script>
</head>
<body class="bg-slate-50 text-slate-900 dark:bg-darkBg dark:text-slate-100 transition-colors duration-300 min-h-screen flex flex-col items-center justify-center p-4">
    <div class="max-w-2xl text-center">
        <div class="w-32 h-32 mx-auto mb-8">
            <img src="/ico.svg" alt="Scholar Navis Logo" class="w-full h-full object-contain drop-shadow-xl" />
        </div>
        <h1 class="text-3xl md:text-5xl font-black mb-6 tracking-tight">macOS Binaries Coming Soon</h1>

        <p class="text-lg text-slate-600 dark:text-slate-400 mb-8 leading-relaxed text-left sm:text-center">
            Pre-compiled binaries are currently published for <b>Windows</b> and <b>Linux</b>
            (both a <b>stable</b> and a <b>dev</b> channel). The <b>macOS</b> standalone release is
            deferred: we have no native hardware for rigorous QA, and shipping an untested bundle
            would be worse than not shipping one at all.<br><br>
            macOS is still fully supported \u2014 just run Scholar Navis directly from the Python source.
        </p>

        <div class="flex flex-col sm:flex-row justify-center gap-4">
            <a href="https://github.com/scholarnavis/scholar_navis/" class="inline-flex items-center justify-center px-8 py-4 bg-primary text-white font-bold rounded-xl hover:scale-105 transition-transform shadow-lg shadow-blue-500/20">
                <i class="fa-brands fa-github mr-2"></i> Run from Source
            </a>
            <a href="https://scholarnavis.com" class="inline-flex items-center justify-center px-8 py-4 bg-white dark:bg-slate-800 text-slate-700 dark:text-slate-200 font-bold rounded-xl border border-slate-200 dark:border-slate-700 hover:bg-slate-50 transition-all">
                Back to Home
            </a>
        </div>
    </div>
</body>
</html>`;

/**
 * 极简落地页：仅在本 Worker 挂在官网域名、且没有静态首页可用时兜底，
 * 避免 `/` 变成自跳自的无限重定向（见 handleRoot）。
 */
const landingHTML = `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Downloads | Scholar Navis</title>
    <link rel="icon" type="image/svg+xml" href="/ico.svg">
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = { darkMode: 'class', theme: { extend: { colors: { primary: '#3b82f6', darkBg: '#0f172a' } } } };
        if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
            document.documentElement.classList.add('dark');
        }
    </script>
</head>
<body class="bg-slate-50 text-slate-900 dark:bg-darkBg dark:text-slate-100 min-h-screen flex flex-col items-center justify-center p-6">
    <img src="/ico.svg" alt="Scholar Navis" class="w-24 h-24 mb-6 drop-shadow-xl" />
    <h1 class="text-3xl font-black mb-2 tracking-tight">Scholar Navis</h1>
    <p class="text-slate-600 dark:text-slate-400 mb-8">Pre-compiled builds for Windows and Linux</p>

    <div class="grid sm:grid-cols-2 gap-4 w-full max-w-2xl">
        <div class="rounded-2xl border border-slate-200 dark:border-slate-700 p-6 bg-white dark:bg-slate-800">
            <h2 class="font-bold text-lg mb-1">Stable</h2>
            <p class="text-sm text-slate-500 dark:text-slate-400 mb-4">Recommended for research work.</p>
            <div class="flex gap-3">
                <a class="px-4 py-2 rounded-lg bg-primary text-white font-semibold" href="/dl?os=windows&amp;channel=stable">Windows</a>
                <a class="px-4 py-2 rounded-lg bg-primary text-white font-semibold" href="/dl?os=linux&amp;channel=stable">Linux</a>
            </div>
        </div>
        <div class="rounded-2xl border border-slate-200 dark:border-slate-700 p-6 bg-white dark:bg-slate-800">
            <h2 class="font-bold text-lg mb-1">Dev</h2>
            <p class="text-sm text-slate-500 dark:text-slate-400 mb-4">Early builds; expect rough edges.</p>
            <div class="flex gap-3">
                <a class="px-4 py-2 rounded-lg border border-primary text-primary font-semibold" href="/dl?os=windows&amp;channel=dev">Windows</a>
                <a class="px-4 py-2 rounded-lg border border-primary text-primary font-semibold" href="/dl?os=linux&amp;channel=dev">Linux</a>
            </div>
        </div>
    </div>

    <a href="https://github.com/scholarnavis/scholar_navis/releases" class="mt-8 text-sm underline text-slate-600 dark:text-slate-400">Release history on GitHub</a>
</body>
</html>`;

/* -------------------------------------------------------------------------- */
/* 版本解析与比较（与 src/core/version.py 的语义保持一致）                     */
/* -------------------------------------------------------------------------- */

/**
 * 解析版本号为可比较的定长数组；无法解析返回 null（排序时垫底）。
 * 支持 `2.0.6`、`2.0.6-dev-1`（本项目用法）、`2.0.6-dev`、`2.2.4-beta-2`、`2.0`。
 */
function versionKey(raw) {
  const match = /^(\d+(?:\.\d+)*)(?:[-_.]?([A-Za-z]+)[-_.]?(\d+)?)?$/.exec(
    String(raw || "").trim()
  );
  if (!match) return null;

  const numbers = match[1].split(".").slice(0, 3).map(Number);
  while (numbers.length < 3) numbers.push(0);

  const stage = (match[2] || "final").toLowerCase();
  const weight = STAGE_WEIGHTS[stage] === undefined ? 0 : STAGE_WEIGHTS[stage];
  return [numbers[0], numbers[1], numbers[2], weight, match[3] ? Number(match[3]) : 0];
}

/** 升序比较：a > b 返回正数，a < b 返回负数，无法解析者视为最小。 */
function compareVersions(a, b) {
  const keyA = versionKey(a);
  const keyB = versionKey(b);
  if (!keyA && !keyB) return 0;
  if (!keyA) return -1;
  if (!keyB) return 1;
  for (let i = 0; i < keyA.length; i += 1) {
    if (keyA[i] !== keyB[i]) return keyA[i] - keyB[i];
  }
  return 0;
}

/* -------------------------------------------------------------------------- */
/* R2 查询                                                                     */
/* -------------------------------------------------------------------------- */

/**
 * 列出该平台该通道下所有合法产物，按版本从新到旧返回 [{version, key}, ...]。
 *
 * 说明：`list` 单次最多 1000 个对象；仓库侧的发布流程每次都会清理同前缀的历史
 * 版本（`build_support/r2_release.py`），因此单页足够。若截断会被显式忽略并
 * 在返回值里标记，避免"悄悄少列一个版本"。
 */
async function listObjects(env, platformTag, channel) {
  const prefix = `${OBJECT_STEM}_${platformTag}_${channel}_v`;
  const listed = await env.scholarnavis.list({ prefix });

  const objects = [];
  for (const item of listed.objects || []) {
    const match = ASSET_RE.exec(item.key);
    if (!match) continue;
    const [, tag, objectChannel, version] = match;
    // 前缀已经过滤过平台/通道，这里再核一遍：旧命名或人工上传的错名对象
    // 不能被当成有效版本对外发布。
    if (tag !== platformTag || objectChannel !== channel) continue;
    if (!versionKey(version)) continue;
    objects.push({ version, key: item.key });
  }

  objects.sort((a, b) => compareVersions(b.version, a.version));
  return { objects, truncated: Boolean(listed.truncated) };
}

async function latestObject(env, platformTag, channel) {
  const { objects } = await listObjects(env, platformTag, channel);
  return objects[0] || null;
}

async function latestVersion(env, platformTag, channel) {
  const latest = await latestObject(env, platformTag, channel);
  return latest ? latest.version : NOT_AVAILABLE;
}

/* -------------------------------------------------------------------------- */
/* 请求辅助                                                                    */
/* -------------------------------------------------------------------------- */

function resolvePlatform(osParam, userAgent) {
  const raw = String(osParam || "").trim().toLowerCase();
  if (raw) return OS_TAGS[raw] || null;   // 明确给了 os 但不认识 → 视为不支持

  const ua = String(userAgent || "");
  if (/windows/i.test(ua)) return "win";
  if (/mac os|macintosh|darwin/i.test(ua)) return "mac";
  if (/linux|x11|android/i.test(ua)) return "linux";
  return null;
}

function normalizeChannel(channelParam) {
  const raw = String(channelParam || "").trim().toLowerCase();
  return CHANNELS.includes(raw) ? raw : "stable";
}

function jsonResponse(data, status, extraHeaders) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json;charset=UTF-8",
      ...CORS_HEADERS,
      ...(extraHeaders || {}),
    },
  });
}

function textResponse(body, status, extraHeaders) {
  return new Response(body, {
    status,
    headers: {
      "Content-Type": "text/plain;charset=UTF-8",
      ...CORS_HEADERS,
      ...(extraHeaders || {}),
    },
  });
}

function htmlResponse(body) {
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/html;charset=UTF-8" },
  });
}

/* -------------------------------------------------------------------------- */
/* 路由处理                                                                    */
/* -------------------------------------------------------------------------- */

/** GET /versions?os=... → 两条通道的最新版本（应用侧更新检查的首选入口）。 */
async function handleVersions(env, platformTag) {
  const channels = {};
  for (const channel of CHANNELS) {
    channels[channel] = platformTag
      ? await latestVersion(env, platformTag, channel)
      : NOT_AVAILABLE;
  }
  return jsonResponse(
    {
      os: platformTag,
      platform_tag: platformTag,
      supported: SUPPORTED_TAGS.includes(platformTag),
      channels,
      generated_at: new Date().toISOString(),
    },
    200
  );
}

/** GET /latest?os=...&channel=... → 纯文本版本号（保留给旧客户端）。 */
async function handleLatest(env, platformTag, channel) {
  if (!platformTag) return textResponse(NOT_AVAILABLE, 200);
  const version = await latestVersion(env, platformTag, channel);
  return textResponse(version, 200);
}

/** GET /dl?os=...&channel=... → 产物附件下载。 */
async function handleDownload(env, platformTag, channel) {
  // macOS（以及任何未知平台）走"敬请期待"页面：这条路由只由浏览器点击触发，
  // 应用本身不会以程序方式请求它（应用只读 /versions）。
  if (!platformTag || !SUPPORTED_TAGS.includes(platformTag)) {
    return htmlResponse(unsupportedOsHTML);
  }

  const latest = await latestObject(env, platformTag, channel);
  if (!latest) {
    return textResponse(
      `No ${channel} release package published for ${platformTag} yet.`,
      404
    );
  }

  const object = await env.scholarnavis.get(latest.key);
  if (object === null) return textResponse("File not found in storage.", 404);

  const headers = new Headers(CORS_HEADERS);
  object.writeHttpMetadata(headers);
  headers.set("etag", object.httpEtag);
  headers.set("Content-Type", "application/zip");
  // 版本号是文件名的一部分，因此同 URL 的内容不会变，可以放心长缓存。
  headers.set("Cache-Control", "public, max-age=31536000, immutable");
  headers.set("Content-Disposition", `attachment; filename="${latest.key}"`);
  return new Response(object.body, { headers });
}

/**
 * GET /changelog?version=... → GitHub Release 的 Markdown 正文。
 *
 * 为什么要代理：应用层直连 api.github.com 在国内网络下经常不可达，而官网可达。
 * 为什么要缓存：未认证的 GitHub API 配额按出口 IP 计（60 次/小时），Cloudflare
 * 的出口 IP 是多个用户共享的，不缓存会让高峰期所有用户一起撞配额。
 */
async function handleChangelog(env, url) {
  const version = String(url.searchParams.get("version") || url.searchParams.get("tag") || "")
    .trim()
    .replace(/^v/i, "");
  if (!version) return jsonResponse({ error: "missing_version" }, 400);

  const repo = String(env.SCHOLAR_NAVIS_REPO || REPO).trim();
  const cache = caches.default;
  const cacheKey = new Request(
    `https://scholar-navis-changelog.internal/${repo}/${version}`,
    { method: "GET" }
  );

  try {
    const cached = await cache.match(cacheKey);
    if (cached) return jsonResponse(await cached.json(), 200, { "X-Cache": "HIT" });
  } catch (error) {
    // 本地调试（wrangler pages dev）没有 cache API，读失败直接走上游。
    console.warn(`changelog cache lookup failed: ${error}`);
  }

  const ghHeaders = {
    Accept: "application/vnd.github+json",
    "User-Agent": "ScholarNavis-ReleaseProxy",
  };
  if (env.GITHUB_TOKEN) ghHeaders.Authorization = `Bearer ${env.GITHUB_TOKEN}`;

  let release = null;
  const direct = await fetch(
    `https://api.github.com/repos/${repo}/releases/tags/v${encodeURIComponent(version)}`,
    { headers: ghHeaders }
  );
  if (direct.ok) {
    release = await direct.json();
  } else if (direct.status !== 404) {
    // 401/403（配额或 token 失效）、5xx 都归为上游故障，让客户端退化为"打开 GitHub 页面"。
    return jsonResponse({ error: "github_unavailable", status: direct.status }, 502);
  }

  if (!release) {
    // 兜底：tag 格式与仓库约定不一致时（例如没有 `v` 前缀），在最近发布里按版本号匹配。
    const listResponse = await fetch(
      `https://api.github.com/repos/${repo}/releases?per_page=50`,
      { headers: ghHeaders }
    );
    if (listResponse.ok) {
      const releases = await listResponse.json();
      release = releases.find(
        (item) => String(item.tag_name || "").replace(/^v/i, "") === version
      ) || null;
    }
  }

  if (!release) return jsonResponse({ error: "not_found", version }, 404);

  const payload = {
    version,
    tag: release.tag_name || `v${version}`,
    name: release.name || "",
    // prerelease 即 dev 通道：创建 Release 时由 build_support/release_notes.py 决定。
    channel: release.prerelease ? "dev" : "stable",
    published_at: release.published_at || null,
    html_url: release.html_url || "",
    body: release.body || "",
  };

  const cacheable = new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json;charset=UTF-8" },
  });
  cacheable.headers.set("Cache-Control", `public, max-age=${CHANGELOG_TTL}`);
  try {
    await cache.put(cacheKey, cacheable);
  } catch (error) {
    // 边缘缓存失败不影响本次响应（例如 cache API 在本地调试环境不可用）。
    console.warn(`changelog cache put failed: ${error}`);
  }

  return jsonResponse(payload, 200, { "X-Cache": "MISS" });
}

async function handleIcon(env) {
  const icon = await env.scholarnavis.get("ico.svg");
  if (icon === null) return textResponse("Icon not found", 404);

  const headers = new Headers();
  icon.writeHttpMetadata(headers);
  headers.set("Content-Type", "image/svg+xml");
  headers.set("Cache-Control", "public, max-age=86400");
  return new Response(icon.body, { headers });
}

/* -------------------------------------------------------------------------- */
/* 入口                                                                        */
/* -------------------------------------------------------------------------- */

/**
 * 根路径/未知路径的处理。
 *
 * 默认跳回官网首页；但如果本 Worker 本身就挂在官网域名上（同一个 host），再跳
 * 一次就是自跳自的无限重定向，此时改为：能拿到静态资源就返回静态首页（Pages
 * 的 `env.ASSETS`），拿不到就给一个极简落地页。
 */
async function handleRoot(request, env, url) {
  if (url.hostname !== new URL(HOME_URL).hostname) {
    return Response.redirect(HOME_URL, 301);
  }

  if (env.ASSETS) {
    const asset = await env.ASSETS.fetch(request);
    if (asset.status !== 404) return asset;
  }
  return htmlResponse(landingHTML);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS_HEADERS });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return textResponse("Method not allowed", 405);
    }

    if (path === "/ico.svg" || path === "/favicon.ico" || path === "/favicon.svg") {
      return handleIcon(env);
    }

    const platformTag = resolvePlatform(url.searchParams.get("os"), request.headers.get("User-Agent"));
    const channel = normalizeChannel(url.searchParams.get("channel"));

    switch (path) {
      case "/versions":
        return handleVersions(env, platformTag);
      case "/latest":
        return handleLatest(env, platformTag, channel);
      case "/dl":
      case "/download":
        return handleDownload(env, platformTag, channel);
      case "/changelog":
        return handleChangelog(env, url);
      default:
        return handleRoot(request, env, url);
    }
  },
};
