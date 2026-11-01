const path = require("node:path");
const { parseCsv } = require("./csv");
const { configuredSession, atomicJson, ROOT } = require("./session");

// Upstream department identifiers; the separate DENZA department is never fetched.
const DEPARTMENTS = [
  {
    key: "caroline-springs",
    id: "memlr9lDsalelogsplusvcg0l581a5",
    name: "BYD Caroline Springs - New",
  },
  {
    key: "nunawading",
    id: "0JKUwJrlXtMg0l581a5",
    name: "BYD Nunawading - New",
  },
];
function currentMonth(now = new Date()) {
  const p = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: "Australia/Melbourne",
      year: "numeric",
      month: "2-digit",
    })
      .formatToParts(now)
      .map((part) => [part.type, part.value]),
  );
  return Number(p.year + p.month);
}
function options(args = []) {
  const monthArgs = args.filter((value) => /^\d{6}$/.test(value));
  if (monthArgs.length > 1) throw new Error("Specify one YYYYMM month.");
  const month = Number(monthArgs[0] || currentMonth());
  if (month < 200001 || month > 210012 || month % 100 < 1 || month % 100 > 12)
    throw new Error("Invalid YYYYMM month.");
  const requested =
    args
      .find((value) => value.startsWith("--sites="))
      ?.slice(8)
      .split(",") || DEPARTMENTS.map((dept) => dept.key);
  if (
    !requested.length ||
    requested.some((key) => !DEPARTMENTS.some((dept) => dept.key === key))
  )
    throw new Error("Only caroline-springs and nunawading are supported.");
  if (
    args.some(
      (value) =>
        !/^\d{6}$/.test(value) &&
        !value.startsWith("--sites=") &&
        !value.startsWith("--output="),
    )
  )
    throw new Error("Unknown SalesLogs command option.");
  const output = args.find((value) => value.startsWith("--output="))?.slice(9);
  return {
    month,
    departments: DEPARTMENTS.filter((dept) => requested.includes(dept.key)),
    output,
  };
}
async function fetchDepartment(session, department, month) {
  const data = await session.getLive({
    yyyyMM: 12,
    id: department.id,
    type: "DEPT",
    mode: "new",
    deleted: "",
    select_yyyyMM: month,
    search: null,
    RO: "",
    Comment: "",
    Backordered: "",
    RegoBackordered: "",
  });
  if (!data || typeof data !== "object" || Array.isArray(data))
    throw new Error(`Invalid SalesLogs response for ${department.name}.`);
  for (const [csv, field] of [
    ["listCsv", "list"],
    ["listTargetCsv", "listTarget"],
    ["listTargetSPCsv", "listTargetSP"],
  ]) {
    if (typeof data[csv] === "string" && data[csv].length)
      data[field] = parseCsv(data[csv]);
  }
  if (
    !Array.isArray(data.list) ||
    data.list.some(
      (row) => !row || typeof row !== "object" || Array.isArray(row),
    )
  )
    throw new Error(`No valid deal list returned for ${department.name}.`);
  return { department: department.name, id: department.id, data };
}
async function fetchSnapshot({
  session,
  month = currentMonth(),
  departments = DEPARTMENTS,
  now = () => new Date(),
}) {
  const result = {
    fetchedAt: now().toISOString(),
    targetMonth: month,
    departments: {},
  };
  // Fail the whole run on either department; never return a partial successful file.
  for (const dept of departments) {
    result.departments[dept.name] = await fetchDepartment(session, dept, month);
    console.log(
      JSON.stringify({
        fetchedDepartment: dept.name,
        month,
        records: result.departments[dept.name].data.list.length,
      }),
    );
  }
  return result;
}
function validateSnapshot(
  payload,
  { month, fresh = true, now = Date.now() } = {},
) {
  if (
    !payload ||
    !payload.departments ||
    (month !== undefined && payload.targetMonth !== month)
  )
    throw new Error("SalesLogs snapshot does not match the requested month.");
  const names = Object.keys(payload.departments);
  if (
    names.length !== DEPARTMENTS.length ||
    names.some((name) => !DEPARTMENTS.some((dept) => dept.name === name))
  )
    throw new Error(
      "Both BYD departments, and no DENZA department, are required.",
    );
  for (const dept of DEPARTMENTS) {
    const entry = payload.departments[dept.name];
    if (entry?.id !== dept.id || !Array.isArray(entry?.data?.list))
      throw new Error(`Missing fresh department data for ${dept.name}.`);
  }
  const age = now - Date.parse(payload.fetchedAt);
  if (
    fresh &&
    (!Number.isFinite(age) || age < -60000 || age > 2 * 60 * 60 * 1000)
  )
    throw new Error("SalesLogs snapshot is stale or has an invalid timestamp.");
  if (
    !DEPARTMENTS.some(
      (dept) => payload.departments[dept.name].data.list.length > 0,
    )
  )
    throw new Error(
      "An empty SalesLogs snapshot was not published or imported.",
    );
}
async function main(args = process.argv.slice(2)) {
  const session = configuredSession();
  const settings = options(args);
  await session.renew();
  const payload = await fetchSnapshot({
    session,
    month: settings.month,
    departments: settings.departments,
  });
  const output = path.resolve(
    ROOT,
    settings.output ||
      process.env.SALESLOGS_OUTPUT_FILE ||
      ".runtime/saleslogs/response_all_departments.json",
  );
  if (settings.departments.length === DEPARTMENTS.length)
    validateSnapshot(payload, { month: settings.month });
  atomicJson(output, payload);
  console.log(
    JSON.stringify({
      status: "fetched",
      snapshot: output,
      month: settings.month,
    }),
  );
  return payload;
}
module.exports = {
  DEPARTMENTS,
  parseCsv,
  currentMonth,
  options,
  fetchDepartment,
  fetchSnapshot,
  validateSnapshot,
  main,
};
if (require.main === module)
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
