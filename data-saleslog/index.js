const https = require("https");
const zlib = require("zlib");
const fs = require("fs");
const path = require("path");

const BASE_URL = "https://app.saleslogs.com";
const API_PATH = "/au/v1/Live/GetLiveAllData";

const COOKIE_STRING = `ListUsersSignIn=devs%40omnisuiteai.com; istrustdevice_xsalelogsslash6m74M4LOwg0l581a5=true; saleslogsmacaddress_xsalelogsslash6m74M4LOwg0l581a5=321c57121ef1b0e7d8a926133bfe47b6; _gcl_au=1.1.492245970.1791724540; _ga=GA1.1.611897315.1791724540; __hstc=133144657.857b1bbc2e79176d327375815bcbac99.1791724540114.1791724540114.1791724540114.1; hubspotutk=857b1bbc2e79176d327375815bcbac99; __hssrc=1; _fbp=fb.1.1791724540878.464550315706945278; intercom-id-d9uyhb16=f3eb85ad-07e3-4b3f-99a0-24de2983f72d; intercom-session-d9uyhb16=; intercom-device-id-d9uyhb16=b3745dca-9c4a-47b9-b32b-a04759326783; SaleslogsIsVerifyGoogle=AB7B0F58E659CADF; ASP.NET_SessionId=czkjaxu2n0ers4skuqistjtv; __hssc=133144657.2.1791724540114; siteCoSelect=all; SaleslogsVersion=DEFAULT; SaleslogsLastMFALastVerify=09/26/2026_Sindh, Pakistan; saleslogsActive=1791724585315; _dd_s=aid=9f69d6f3-65c6-45ea-9ea2-86dbd0a77088&rum=2&id=3e740ed7-b075-4c7c-9449-56e98d33d911&created=1791724555180&expire=1791725489925; _ga_XKMWVTW88E=GS2.1.s1791724539$o1$g1$t1791724590$j9$l0$h0; ai_user=2rvZk|2026-10-11T13:16:35.731Z; AUTHSALESLOGS={"tokenId":"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1bmlxdWVfbmFtZSI6IjYxNDA5Iiwicm9sZSI6IlNNIiwiYWNjZXNzR3JvdXAiOiJ7XCJzYWxlXCI6dHJ1ZSxcImZpbmFuY2VcIjpmYWxzZSxcImFmdGVybWFya2V0XCI6ZmFsc2UsXCJ0cmFkZWluXCI6ZmFsc2V9IiwiVXNlcklkIjoiNjE0MDkiLCJSb2xlSWQiOiIyIiwiUm9sZUlkTmFtZSI6IlNhbGVzIE1hbmFnZXIiLCJSb2xlSW5pdGlhbCI6IlNNIiwiUm9sZUxldmVsIjoiNSIsIlVzZXJQQ29tcElkIjoiMTU0NiIsIkJldGFTaWduaW4iOiJGIiwiU1BTaWduaW4iOiJGIiwiSXNTaG93Q2FsY3VsYXRvcnMiOiJGIiwiU1BSZXN0cmljdGVkTm9uZSI6IkYiLCJJc1Jlc3RyaWN0ZWRDTyI6ImZhbHNlIiwiSXNSZXN0cmljdGVkIjoiRiIsIklzTmV3VmVyc2lvbiI6Ik4iLCJTb3VyY2VWZXJzaW9uIjoiREVGQVVMVCIsIklzVGVtcFBhc3N3b3JkIjoiZmFsc2UiLCJTZXNzaW9uSUQiOiJjemtqYXh1Mm4wZXJzNHNrdXFpc3RqdHYiLCJuYmYiOjE3OTE3MjQ1ODgsImV4cCI6MTc5MTgxMDk4OCwiaWF0IjoxNzkxNzI0NTg4fQ.GvKMGbP9EdkBXDzo0pFRS2lYoEqOQ8gqmZbaqwx5lz4","role":"SM" }; fs_lua=1.1791724595496; fs_uid=#XM0G5#8a6cf07e-e7ba-41f5-b0fc-9aac75ab2195:cf4f8961-b606-4432-82d7-92966de92d80:1791724532495::3#52bff52a###/1820149643; checkShowTempPasswordxsalelogsslash6m74M4LOwg0l581a5=1; ai_session=/+0Xv|1791724597666.8|1791724597666.8`;

// ─── DEPARTMENTS ──────────────────────────────────────────────────────────────
const DEPARTMENTS = [
  { id: "memlr9lDsalelogsplusvcg0l581a5", name: "BYD Caroline Springs - New" },
  { id: "9fMiqBnJZjog0l581a5", name: "BYD Nunawading - New" },
  { id: "0JKUwJrlXtMg0l581a5", name: "Denza Melbourne - New" },
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
    req.write(payload);
    req.end();
  });
}

// ─── FETCH ONE DEPARTMENT ─────────────────────────────────────────────────────
async function fetchDepartment(dept, month) {
  const targetMonth = month || (process.argv[2] ? parseInt(process.argv[2], 10) : 202610);
  const body = {
    yyyyMM: 12,
    id: dept.id,
    type: "DEPT",
    mode: "new",
    deleted: "",
    select_yyyyMM: targetMonth,
    search: null,
    RO: "",
    Comment: "",
    Backordered: "",
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
          if (typeof val === "string") console.log(`     ${key}: [string, ${val.length} chars]`);
          else if (Array.isArray(val)) console.log(`     ${key}: [array, ${val.length} items]`);
          else console.log(`     ${key}:`, val);
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