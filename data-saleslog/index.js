const https = require("https");
const zlib = require("zlib");
const fs = require("fs");
const path = require("path");

const BASE_URL = "https://app.saleslogs.com";
const API_PATH = "/au/v1/Live/GetLiveAllData";

const COOKIE_STRING = `
ListUsersSignIn=devs%40omnisuiteai.com; istrustdevice_xsalelogsslash6m74M4LOwg0l581a5=true; saleslogsmacaddress_xsalelogsslash6m74M4LOwg0l581a5=321c57121ef1b0e7d8a926133bfe47b6; hubspotutk=505b1b6e6c53057f674c6f2093956d1e; ASP.NET_SessionId=wr5ve1s1cgzltjy121gx5cee; fs_lua=1.1792914464511; AUTHSALESLOGS=%7B%22tokenId%22%3A%22eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1bmlxdWVfbmFtZSI6IjYxNDA5Iiwicm9sZSI6IlNNIiwiYWNjZXNzR3JvdXAiOiJ7XCJzYWxlXCI6dHJ1ZSxcImZpbmFuY2VcIjpmYWxzZSxcImFmdGVybWFya2V0XCI6ZmFsc2UsXCJ0cmFkZWluXCI6ZmFsc2V9IiwiVXNlcklkIjoiNjE0MDkiLCJSb2xlSWQiOiIyIiwiUm9sZUlkTmFtZSI6IlNhbGVzIE1hbmFnZXIiLCJSb2xlSW5pdGlhbCI6IlNNIiwiUm9sZUxldmVsIjoiNSIsIlVzZXJQQ29tcElkIjoiMTU0NiIsIkJldGFTaWduaW4iOiJGIiwiU1BTaWduaW4iOiJGIiwiSXNTaG93Q2FsY3VsYXRvcnMiOiJGIiwiU1BSZXN0cmljdGVkTm9uZSI6IkYiLCJJc1Jlc3RyaWN0ZWRDTyI6ImZhbHNlIiwiSXNSZXN0cmljdGVkIjoiRiIsIklzTmV3VmVyc2lvbiI6Ik4iLCJJc1RlbXBQYXNzd29yZCI6ImZhbHNlIiwibmJmIjoxNzkyOTE0NDczLCJleHAiOjE3OTMwMDA4NzMsImlhdCI6MTc5MjkxNDQ3M30.XOWIi9-UULC-Ai8z_5oS-D-Nb3-mf55zpms1qa0NgKU%22%2C%22role%22%3A%22SM%22%7D; SaleslogsIsVerifyGoogle=1; SaleslogsLastMFALastVerify=09%2F26%2F2026_Sindh%2C%20Pakistan; fs_uid=#XM0G5#02504396-df4a-4be9-b213-7e89fb20ec25:200496a0-900d-4fcc-a951-ced9f7859c19:1792914464511::1#52bff52a###/1824450467; __hstc=133144657.505b1b6e6c53057f674c6f2093956d1e.1792607140587.1792909669396.1792914475741.3; __hssrc=1; __hssc=133144657.1.1792914475741; intercom-device-id-d9uyhb16=186a2215-55e8-4c48-9555-ead5b7d77d36; intercom-session-d9uyhb16=WTdVcFRhMU9HMnY0UWZRcC9EcTlsUkJrdFVKazBvejk3UVBGQWkvcEZtU2d0UXM5bVhFRVI0SGpiZzdhdFlxelEwNlZOcHdSWTZHZXFBS1A1NExPaml4eUV1ZTlFRHpMOS9ZNXN4YXFibWE3em5nMGRlWlQ3ZmJ1TU9lb2p6dmFaSVNDRUV4SDRzTWJSbGM3a2ZWVVczTllVNEZ2VFpWSVZJU0pxOEJkdm4rZlNkNFZ3VkcxQW05WVRwQ09HOHVvYjJuYXBDL2N3Z201WEZkWmZXRXBId0dEMVVUd1QwQ3E3YnRNWFl5L2NzOGpNY2pxQXVwTk5lV0I5eVR4d2Y5UXdvVk1xVTlzUEUxakhWeENUK1VBbEE9PS0tVHA4WE9GRE1tcUwrSlk2SUNkK1NZUT09--6541ec4eac7a2270114ec0a8ce9d51fa283d916d; _dd_s=aid=c05edd9b-cc47-46fd-baca-549c9ede3a34&rum=2&id=0ed933b9-b9c3-489d-b41c-6ede3237e85b&created=1792914700198&expire=1792915600199; saleslogsActive=1792914700992`;

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
