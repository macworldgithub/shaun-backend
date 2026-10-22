const https = require("https");
const zlib = require("zlib");
const fs = require("fs");
const path = require("path");

const BASE_URL = "https://app.saleslogs.com";
const API_PATH = "/au/v1/Live/GetLiveAllData";

const COOKIE_STRING = `ListUsersSignIn=devs%40omnisuiteai.com; istrustdevice_xsalelogsslash6m74M4LOwg0l581a5=true; saleslogsmacaddress_xsalelogsslash6m74M4LOwg0l581a5=321c57121ef1b0e7d8a926133bfe47b6; intercom-id-d9uyhb16=7b9723ae-b3ca-4d46-a035-12084798ed10; intercom-session-d9uyhb16=; intercom-device-id-d9uyhb16=9575e3db-6676-48c6-98cd-5d223b16d38e; _gcl_au=1.1.1997415307.1792607140; _ga=GA1.1.684333449.1792607140; __hstc=133144657.505b1b6e6c53057f674c6f2093956d1e.1792607140587.1792607140587.1792607140587.1; hubspotutk=505b1b6e6c53057f674c6f2093956d1e; __hssrc=1; _fbp=fb.1.1792607141417.903557414856763666; SaleslogsIsVerifyGoogle=1; ASP.NET_SessionId=jonqjdn2lawk3hz5kchenbto; siteCoSelect=all; SaleslogsVersion=DEFAULT; SaleslogsLastMFALastVerify=09/26/2026_Sindh, Pakistan; __hssc=133144657.2.1792607140587; saleslogsActive=1792608279524; _ga_XKMWVTW88E=GS2.1.s1792607139$o1$g1$t1792608286$j36$l0$h0; _dd_s=aid=3c87ebb6-54ef-4d3d-9c9a-749cd365cf56&rum=2&id=a5030c7d-bc48-4b3b-8ebb-b6b8edcb49b3&created=1792608269280&expire=1792609186684; ai_user=NJLhFAFRZAB8fptXjjrAmJ|2026-10-21T18:44:51.117Z; checkShowTempPasswordxsalelogsslash6m74M4LOwg0l581a5=1; ai_session=TYh7zQW2zH9luvRgddT7m3|1792608293422|1792608293422; fs_lua=1.1792608293439; fs_uid=#XM0G5#f171aeac-4d94-487c-a73c-dca89f1fa030:41eebb92-d31e-48c9-9a83-b2d079e1fa2a:1792607137020::3#52bff52a###/1824143144; AUTHSALESLOGS={"tokenId":"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1bmlxdWVfbmFtZSI6IjYxNDA5Iiwicm9sZSI6IlNNIiwiYWNjZXNzR3JvdXAiOiJ7XCJzYWxlXCI6dHJ1ZSxcImZpbmFuY2VcIjpmYWxzZSxcImFmdGVybWFya2V0XCI6ZmFsc2UsXCJ0cmFkZWluXCI6ZmFsc2V9IiwiVXNlcklkIjoiNjE0MDkiLCJSb2xlSWQiOiIyIiwiUm9sZUlkTmFtZSI6IlNhbGVzIE1hbmFnZXIiLCJSb2xlSW5pdGlhbCI6IlNNIiwiUm9sZUxldmVsIjoiNSIsIlVzZXJQQ29tcElkIjoiMTU0NiIsIkJldGFTaWduaW4iOiJGIiwiU1BTaWduaW4iOiJGIiwiSXNTaG93Q2FsY3VsYXRvcnMiOiJGIiwiU1BSZXN0cmljdGVkTm9uZSI6IkYiLCJJc1Jlc3RyaWN0ZWRDTyI6ImZhbHNlIiwiSXNSZXN0cmljdGVkIjoiRiIsIklzTmV3VmVyc2lvbiI6Ik4iLCJTb3VyY2VWZXJzaW9uIjoiREVGQVVMVCIsIklzVGVtcFBhc3N3b3JkIjoiZmFsc2UiLCJTZXNzaW9uSUQiOiJqb25xamRuMmxhd2szaHo1a2NoZW5idG8iLCJuYmYiOjE3OTI2MDgyOTksImV4cCI6MTc5MjY5NDY5OSwiaWF0IjoxNzkyNjA4Mjk5fQ.nlITFmjGqgTBpTEd9LWzgq_qLhnDk-9sxV3QCyzcshk","role":"SM" }; saleslogs_monthSelect=202610; saleslogs_monthLast=0`;

// ─── DEPARTMENTS ──────────────────────────────────────────────────────────────
const DEPARTMENTS = [
  { id: "memlr9lDsalelogsplusvcg0l581a5", name: "BYD Caroline Springs - New" },
  { id: "9fMiqBnJZjog0l581a5", name: "BYD Nunawading - New" },
  { id: "0JKUwJrlXtMg0l581a5", name: "Denza Melbourne - New" },
];

const monthArg = process.argv.slice(2).find(arg => /^\d{6}$/.test(arg));
const targetMonth = monthArg ? Number(monthArg) : 202610;
const sitesArg = process.argv.slice(2).find(arg => arg.startsWith("--sites="));
const siteNames = {
  "caroline-springs": "BYD Caroline Springs - New",
  "nunawading": "BYD Nunawading - New",
  "denza-melbourne": "Denza Melbourne - New",
};
const requestedSites = sitesArg ? sitesArg.slice("--sites=".length).split(",") : Object.keys(siteNames);
if (requestedSites.some(site => !siteNames[site])) {
  throw new Error("Unknown --sites value. Use caroline-springs,nunawading,denza-melbourne.");
}
const selectedDepartments = DEPARTMENTS.filter(dept => requestedSites.some(site => siteNames[site] === dept.name));

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
        "Content-Type": "application/json; charset=UTF-8",
        "Content-Length": Buffer.byteLength(payload),
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Origin": BASE_URL,
        "Referer": `${BASE_URL}/au/v1/Live`,
        "User-Agent": "Mozilla/5.0 (Linux; Android 16; Pixel 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": buildCookieHeader(),
      },
    }, (res) => {
      if (res.statusCode < 200 || res.statusCode >= 300) {
        res.resume();
        reject(new Error(`SalesLogs returned HTTP ${res.statusCode}`));
        return;
      }
      parseCookies(res.headers["set-cookie"]);

      const enc = res.headers["content-encoding"];
      let stream = res;
      if (enc === "gzip") stream = res.pipe(zlib.createGunzip());
      if (enc === "deflate") stream = res.pipe(zlib.createInflate());
      if (enc === "br") stream = res.pipe(zlib.createBrotliDecompress());

      const chunks = [];
      stream.on("data", c => chunks.push(c));
      stream.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
      stream.on("error", reject);
    });
    req.on("error", reject);
    req.setTimeout(30000, () => req.destroy(new Error("SalesLogs request timed out")));
    req.write(payload);
    req.end();
  });
}

// ─── FETCH ONE DEPARTMENT ─────────────────────────────────────────────────────
async function fetchDepartment(dept, month) {
  const requestedMonth = month || targetMonth;
  const body = {
    yyyyMM: 12,
    id: dept.id,
    type: "DEPT",
    mode: "new",
    deleted: "",
    select_yyyyMM: requestedMonth,
    search: null,
    RO: "",
    Comment: "",
    Backordered: "",
    RegoBackordered: "",
  };

  console.log(`\n📡 Fetching: ${dept.name} (${dept.id}) [Month: ${requestedMonth}]`);
  const raw = await apiRequest(body);

  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error(`Non-JSON response for ${dept.name}; fresh data was not fetched`);
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
  if (!parsed || !Array.isArray(parsed.list)) {
    throw new Error(`No deal list returned for ${dept.name}; check the SalesLogs session`);
  }

  return { department: dept.name, id: dept.id, data: parsed };
}

// ─── MAIN ─────────────────────────────────────────────────────────────────────
(async () => {
  const results = {};
  const timestamp = new Date().toISOString();
  for (const dept of selectedDepartments) {
    try {
      const result = await fetchDepartment(dept, targetMonth);
      results[dept.name] = result;

      // Print summary per department
      const d = result.data;
      if (typeof d === "object" && d !== null) {
        console.log(`  ✅ Keys: ${Object.keys(d).join(", ")}`);
        for (const [key, val] of Object.entries(d)) {
          if (typeof val === "string") console.log(`     ${key}: [string, ${val.length} chars]`);
          else if (Array.isArray(val)) console.log(`     ${key}: [array, ${val.length} items]`);
          else console.log(`     ${key}: [${typeof val}]`);
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
      process.exitCode = 1;
    }

    // Small delay between requests to avoid rate limiting
    await new Promise(r => setTimeout(r, 500));
  }

  // Save only the departments explicitly requested for this run.
  const combinedFile = path.join(__dirname, "response_all_departments.json");
  fs.writeFileSync(combinedFile, JSON.stringify({ fetchedAt: timestamp, targetMonth, departments: results }, null, 2), "utf-8");
  console.log(`\n✅ All done! Combined file saved to ${combinedFile}`);
})();
