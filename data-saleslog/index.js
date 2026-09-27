const https = require("https");
const zlib = require("zlib");
const fs = require("fs");

const BASE_URL = "https://app.saleslogs.com";
const API_PATH = "/au/v1/Live/GetLiveAllData";

const COOKIE_STRING = `_ga=GA1.1.420777265.1790318978; SALESLOGSAUTH=%7B%22tokenId%22%3A%22eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1bmlxdWVfbmFtZSI6IjYxNDA5Iiwicm9sZSI6IlNNIiwiYWNjZXNzR3JvdXAiOiJ7XCJzYWxlXCI6dHJ1ZSxcImZpbmFuY2VcIjpmYWxzZSxcImFmdGVybWFya2V0XCI6ZmFsc2UsXCJ0cmFkZWluXCI6ZmFsc2V9IiwibmJmIjoxNzkwNTIwNDM3LCJleHAiOjE3OTA2MDY4MzcsImlhdCI6MTc5MDUyMDQzN30.T3txud_MENi8VIEXPW2G8XkRBnQER8AXl0NfQm2xGo0%22%2C%22role%22%3A%22SM%22%7D; ASP.NET_SessionId=rjcc1cnyhfwcpqlws4bg0x32; USER1=UserId=xsalelogsslash6m74M4LOwg0l581a5&UserIdHex=C7FEA6EF83382CEC&RoleId=UMJLgjb8yB0g0l581a5&PermissionId=GuaHHjVrf1Ag0l581a5&UserPCompId=RWGsnlDp1rcg0l581a5; USER2=RoleIdName=Sales Manager&RoleInitial=SM&RoleLevel=GuaHHjVrf1Ag0l581a5&PermissionIdName=User Administrator&BetaSignin=F; USER4=UserApi=a7rPl8C8IFel4431fsalelogsplus1Q3xONpMMrrCdb_api_142a0605c46c048ac0aca8e980d6b759&SessionID=rjcc1cnyhfwcpqlws4bg0x32&SingedUp=1788450666&LastSeen=1790520446&FirstSeen=1788451164&UserLastLogin=9/28/2026 12:47:17 AM; USER5=SPSignin=F&IsShowCalculators=F&SPRestrictedNone=F&IsRestrictedCO=false&UserInitialAccess=&UserIdAccess=H4oxMTElK1Ug0l581a5&IsRestricted=F&IsNewVersion=N&LandingPage=`;

// ─── DEPARTMENTS ──────────────────────────────────────────────────────────────
const DEPARTMENTS = [
  { id: "memlr9lDsalelogsplusvcg0l581a5", name: "BYD Caroline Springs - New" },
  { id: "9fMiqBnJZjog0l581a5",            name: "BYD Nunawading - New"        },
  { id: "0JKUwJrlXtMg0l581a5",            name: "Denza Melbourne - New"       },
];

// ─── COOKIE JAR ──────────────────────────────────────────────────────────────
const cookieJar = new Map();

function parseCookies(headers) {
  for (const h of (headers || [])) {
    const [nv] = h.split(";");
    const idx = nv.indexOf("=");
    cookieJar.set(nv.slice(0, idx).trim(), nv.slice(idx + 1).trim());
  }
}

function buildCookieHeader() {
  const initial = {};
  for (const part of COOKIE_STRING.split(";")) {
    const idx = part.indexOf("=");
    if (idx === -1) continue;
    initial[part.slice(0, idx).trim()] = part.slice(idx + 1).trim();
  }
  return Object.entries({ ...initial, ...Object.fromEntries(cookieJar) })
    .map(([k, v]) => `${k}=${v}`).join("; ");
}

// ─── REQUEST ─────────────────────────────────────────────────────────────────
async function apiRequest(body) {
  const payload = JSON.stringify(body);
  return new Promise((resolve, reject) => {
    const req = https.request({
      hostname: "app.saleslogs.com",
      port: 443,
      path: API_PATH,
      method: "POST",
      headers: {
        "Content-Type":    "application/json; charset=UTF-8",
        "Content-Length":  Buffer.byteLength(payload),
        "Accept":          "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control":   "no-cache",
        "Origin":          BASE_URL,
        "Referer":         `${BASE_URL}/au/v1/Live`,
        "User-Agent":      "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Mobile Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie":          buildCookieHeader(),
      },
    }, (res) => {
      parseCookies(res.headers["set-cookie"]);

      const enc = res.headers["content-encoding"];
      let stream = res;
      if (enc === "gzip")    stream = res.pipe(zlib.createGunzip());
      if (enc === "deflate") stream = res.pipe(zlib.createInflate());
      if (enc === "br")      stream = res.pipe(zlib.createBrotliDecompress());

      const chunks = [];
      stream.on("data", c => chunks.push(c));
      stream.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
      stream.on("error", reject);
    });
    req.on("error", reject);
    req.write(payload);
    req.end();
  });
}

// ─── FETCH ONE DEPARTMENT ─────────────────────────────────────────────────────
async function fetchDepartment(dept) {
  const body = {
    yyyyMM:          12,
    id:              dept.id,
    type:            "DEPT",
    mode:            "new",
    deleted:         "",
    select_yyyyMM:   202609,
    search:          null,
    RO:              "",
    Comment:         "",
    Backordered:     "",
    RegoBackordered: "",
  };

  console.log(`\n📡 Fetching: ${dept.name} (${dept.id})`);
  const raw = await apiRequest(body);

  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    console.log(`  ⚠️  Non-JSON response for ${dept.name}`);
    parsed = raw;
  }

  return { department: dept.name, id: dept.id, data: parsed };
}

// ─── MAIN ─────────────────────────────────────────────────────────────────────
(async () => {
  const results = {};
  const timestamp = new Date().toISOString();

  for (const dept of DEPARTMENTS) {
    try {
      const result = await fetchDepartment(dept);
      results[dept.name] = result;

      // Print summary per department
      const d = result.data;
      if (typeof d === "object" && d !== null) {
        console.log(`  ✅ Keys: ${Object.keys(d).join(", ")}`);
        for (const [key, val] of Object.entries(d)) {
          if (typeof val === "string")      console.log(`     ${key}: [string, ${val.length} chars]`);
          else if (Array.isArray(val))      console.log(`     ${key}: [array, ${val.length} items]`);
          else                              console.log(`     ${key}:`, val);
        }
      }

      // Save individual file per department
      const safeName = dept.name.replace(/[^a-z0-9]/gi, "_").toLowerCase();
      const filename = `response_${safeName}.json`;
      fs.writeFileSync(filename, JSON.stringify({ fetchedAt: timestamp, department: dept.name, id: dept.id, data: d }, null, 2), "utf-8");
      console.log(`  💾 Saved to ${filename}`);

    } catch (err) {
      console.error(`  ❌ Failed for ${dept.name}: ${err.message}`);
      results[dept.name] = { error: err.message };
    }

    // Small delay between requests to avoid rate limiting
    await new Promise(r => setTimeout(r, 500));
  }

  // Save combined file with all 3 departments
  const combinedFile = "response_all_departments.json";
  fs.writeFileSync(combinedFile, JSON.stringify({ fetchedAt: timestamp, departments: results }, null, 2), "utf-8");
  console.log(`\n✅ All done! Combined file saved to ${combinedFile}`);
})();