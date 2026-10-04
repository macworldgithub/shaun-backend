const https = require("https");
const zlib = require("zlib");
const fs = require("fs");
const path = require("path");

const BASE_URL = "https://app.saleslogs.com";
const API_PATH = "/au/v1/Live/GetLiveAllData";

const COOKIE_STRING = `_ga=GA1.1.420777265.1790318978; _gcl_au=1.1.1312080609.1790318978; hubspotutk=2f927912b6d7d04273e104de56c66a14; _fbp=fb.1.1790319016784.487744465716122947; ListUsersSignIn=devs%40omnisuiteai.com; intercom-id-d9uyhb16=d08ee207-2a26-4a4c-81e0-03860a33ede0; intercom-device-id-d9uyhb16=0bb6ee2a-a56c-4740-a942-d1fdc4aedeb9; istrustdevice_xsalelogsslash6m74M4LOwg0l581a5=true; saleslogsmacaddress_xsalelogsslash6m74M4LOwg0l581a5=321c57121ef1b0e7d8a926133bfe47b6; ai_user=ks+8Y|2026-09-27T14:47:51.335Z; __hstc=133144657.2f927912b6d7d04273e104de56c66a14.1790319013921.1790531607954.1791097787905.6; __hssrc=1; SALESLOGSAUTH=%7B%22tokenId%22%3A%22eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1bmlxdWVfbmFtZSI6IjYxNDA5Iiwicm9sZSI6IlNNIiwiYWNjZXNzR3JvdXAiOiJ7XCJzYWxlXCI6dHJ1ZSxcImZpbmFuY2VcIjpmYWxzZSxcImFmdGVybWFya2V0XCI6ZmFsc2UsXCJ0cmFkZWluXCI6ZmFsc2V9IiwibmJmIjoxNzkxMDk3ODA1LCJleHAiOjE3OTExODQyMDUsImlhdCI6MTc5MTA5NzgwNX0.XWONStJakbhat9E6CZ6YFXtpcrrRwY5pgt65ZUWl6oo%22%2C%22role%22%3A%22SM%22%7D; SALESLOGSAUTHLOGIN=User%20Administrator; ASP.NET_SessionId=vwxmpnrjwgnbf10s2yzktg0d; __hssc=133144657.2.1791097787905; siteCoSelect=all; USER1=UserId=xsalelogsslash6m74M4LOwg0l581a5&UserIdHex=C7FEA6EF83382CEC&RoleId=UMJLgjb8yB0g0l581a5&PermissionId=GuaHHjVrf1Ag0l581a5&UserPCompId=RWGsnlDp1rcg0l581a5; USER2=RoleIdName=Sales Manager&RoleInitial=SM&RoleLevel=GuaHHjVrf1Ag0l581a5&PermissionIdName=User Administrator&BetaSignin=F; USER3=UserFirstName=GyVChYWDeWkg0l581a5&UserLastName=NWuKwrtSGgCtJfFV51lzkAg0l581a5g0l581a5&UserInitial=xsalelogsslash6m74M4LOwg0l581a5&UserEmail=a7rPl8C8IFel4431fsalelogsplus1Q3xONpMMrrCdb&UserLandlinePhone=&UserMobilePhone=+61499918523&IsTempPassword=false; USER4=UserApi=a7rPl8C8IFel4431fsalelogsplus1Q3xONpMMrrCdb_api_142a0605c46c048ac0aca8e980d6b759&SessionID=vwxmpnrjwgnbf10s2yzktg0d&SingedUp=1788450666&LastSeen=1791097813&FirstSeen=1788451164&UserLastLogin=10/4/2026 6:10:05 PM; USER5=SPSignin=F&IsShowCalculators=F&SPRestrictedNone=F&IsRestrictedCO=false&UserInitialAccess=&UserIdAccess=H4oxMTElK1Ug0l581a5&IsRestricted=F&IsNewVersion=N&LandingPage=; SaleslogsVersion=DEFAULT; SaleslogsIsVerifyGoogle=AB7B0F58E659CADF; SaleslogsLastMFALastVerify=09/26/2026_Sindh, Pakistan; intercom-session-d9uyhb16=T01xcXEvUTYwSmpuK3RnekJRNThsRjlnVml2MThWem03VjBKa3ZzT2tnaG5WZmRJNlNWY09meWk3cFZYd0ZlVVpPMkM1K2h1VDg4cWNrRVlLYkRvOW45NHN0M3JvTTQvSkFGbE5sR0oweUdZdDN2c29TQzBSNDY5MGFVc2JaRWlkU2t6VmM3T3FtMnhNV1RYRFI4SzdzdEV3Y04zS2h1RlRpN011OVhQclErT0ZkTWxEQlBkWitiSHlvQ1JqOEREV0Zra2EreU16ZGNnckZQREt6WmZuV0VLNkd6MzR4VVZ0Qk9RNitlSEUwalJ6eW1LMHlOS1RCTWN1K1pqcWFyaHd3WTVNRTJjeU5OVnd3UmJNZlFnNGc9PS0tVlQ2eTRrTzhNcTBKUndMem1IeUdydz09--9c1bc3924db183ef73f2f7c25431d10714224ea3; saleslogsActive=1791098023531; _dd_s=aid=907d8360-c3c2-4358-8194-52ee80b85092&rum=2&id=8b7c5bc4-1635-409b-adf2-6786e6fcd2de&created=1791097794342&expire=1791098932359; _ga_XKMWVTW88E=GS2.1.s1791097786$o8$g1$t1791098033$j60$l0$h0; checkShowTempPasswordxsalelogsslash6m74M4LOwg0l581a5=1; fs_lua=1.1791098044364; fs_uid=#XM0G5#8a6cf07e-e7ba-41f5-b0fc-9aac75ab2195:f2d29519-c254-4bf0-a500-2ce085cc7a30:1791097779330::3#52bff52a###/1820149635`;

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
    if (idx !== -1) {
      cookieJar.set(nv.slice(0, idx).trim(), nv.slice(idx + 1).trim());
    }
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

// ─── CSV PARSER (Zero Dependency) ─────────────────────────────────────────────
// SalesLogs returns deals inside `listCsv`. We decode this directly into `list`.
function parseCsv(csvText) {
  if (!csvText || typeof csvText !== "string") return [];
  const rows = [];
  let currentRow = [];
  let currentField = "";
  let insideQuotes = false;
  
  for (let i = 0; i < csvText.length; i++) {
    const char = csvText[i];
    const nextChar = csvText[i + 1];
    
    if (char === '"') {
      if (insideQuotes && nextChar === '"') {
        currentField += '"';
        i++;
      } else {
        insideQuotes = !insideQuotes;
      }
    } else if (char === ',' && !insideQuotes) {
      currentRow.push(currentField);
      currentField = "";
    } else if ((char === '\r' || char === '\n') && !insideQuotes) {
      if (char === '\r' && nextChar === '\n') {
        i++;
      }
      currentRow.push(currentField);
      currentField = "";
      if (currentRow.length > 1 || (currentRow.length === 1 && currentRow[0] !== "")) {
        rows.push(currentRow);
      }
      currentRow = [];
    } else {
      currentField += char;
    }
  }
  if (currentField.length > 0 || currentRow.length > 0) {
    currentRow.push(currentField);
    rows.push(currentRow);
  }
  
  if (rows.length < 2) return [];
  const headers = rows[0].map(h => h.trim());
  return rows.slice(1).map(row => {
    const obj = {};
    for (let j = 0; j < headers.length; j++) {
      obj[headers[j]] = row[j] !== undefined ? row[j] : "";
    }
    return obj;
  });
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
        "User-Agent":      "Mozilla/5.0 (Linux; Android 16; Pixel 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36",
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
async function fetchDepartment(dept, month) {
  const targetMonth = month || (process.argv[2] ? parseInt(process.argv[2], 10) : 202610);
  const body = {
    yyyyMM:          12,
    id:              dept.id,
    type:            "DEPT",
    mode:            "new",
    deleted:         "",
    select_yyyyMM:   targetMonth,
    search:          null,
    RO:              "",
    Comment:         "",
    Backordered:     "",
    RegoBackordered: "",
  };

  console.log(`\n📡 Fetching: ${dept.name} (${dept.id}) [Month: ${targetMonth}]`);
  const raw = await apiRequest(body);

  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    console.log(`  ⚠️  Non-JSON response for ${dept.name}`);
    parsed = raw;
  }

  // Decompress SalesLogs columnar CSV format into parsed.list
  if (parsed && typeof parsed.listCsv === "string" && parsed.listCsv.length > 0) {
    const records = parseCsv(parsed.listCsv);
    console.log(`  📦 Unpacked listCsv into ${records.length} deal records for ${dept.name}`);
    parsed.list = records;
  }
  if (parsed && typeof parsed.listTargetCsv === "string" && parsed.listTargetCsv.length > 0) {
    parsed.listTarget = parseCsv(parsed.listTargetCsv);
  }
  if (parsed && typeof parsed.listTargetSPCsv === "string" && parsed.listTargetSPCsv.length > 0) {
    parsed.listTargetSP = parseCsv(parsed.listTargetSPCsv);
  }

  return { department: dept.name, id: dept.id, data: parsed };
}

// ─── MAIN ─────────────────────────────────────────────────────────────────────
(async () => {
  const results = {};
  const timestamp = new Date().toISOString();
  const targetMonth = process.argv[2] ? parseInt(process.argv[2], 10) : 202610;

  for (const dept of DEPARTMENTS) {
    try {
      const result = await fetchDepartment(dept, targetMonth);
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

        // Save raw CSV file as well if listCsv was returned
        if (typeof d.listCsv === "string" && d.listCsv.length > 0) {
          const safeName = dept.name.replace(/[^a-z0-9]/gi, "_").toLowerCase();
          const csvFilename = path.join(__dirname, `response_${safeName}.csv`);
          fs.writeFileSync(csvFilename, d.listCsv, "utf-8");
          console.log(`  💾 Saved CSV to ${csvFilename}`);
        }
      }

      // Save individual JSON file per department
      const safeName = dept.name.replace(/[^a-z0-9]/gi, "_").toLowerCase();
      const filename = path.join(__dirname, `response_${safeName}.json`);
      fs.writeFileSync(filename, JSON.stringify({ fetchedAt: timestamp, department: dept.name, id: dept.id, data: d }, null, 2), "utf-8");
      console.log(`  💾 Saved JSON to ${filename}`);

    } catch (err) {
      console.error(`  ❌ Failed for ${dept.name}: ${err.message}`);
      results[dept.name] = { error: err.message };
    }

    // Small delay between requests to avoid rate limiting
    await new Promise(r => setTimeout(r, 500));
  }

  // Save combined file with all 3 departments
  const combinedFile = path.join(__dirname, "response_all_departments.json");
  fs.writeFileSync(combinedFile, JSON.stringify({ fetchedAt: timestamp, targetMonth, departments: results }, null, 2), "utf-8");
  console.log(`\n✅ All done! Combined file saved to ${combinedFile}`);
})();