const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const { configuredSession, atomicJson, ROOT } = require("./session");
const { fetchSnapshot, validateSnapshot, currentMonth } = require("./index");

function execute(command, args, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: ROOT,
      env,
      stdio: "inherit",
      windowsHide: true,
    });
    child.once("error", () =>
      reject(new Error("Could not start the SalesLogs importer.")),
    );
    child.once("exit", (code, signal) =>
      code === 0
        ? resolve()
        : reject(new Error(`SalesLogs importer failed (${signal || code}).`)),
    );
  });
}
async function run({
  session,
  directory,
  python,
  env = process.env,
  fetch = fetchSnapshot,
  command = execute,
  month = currentMonth(),
}) {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const backupDir = path.join(directory, "backups");
  fs.mkdirSync(backupDir, { recursive: true, mode: 0o700 });
  const startedAt = new Date().toISOString(),
    runId = startedAt.replace(/[:.]/g, "-") + "-" + crypto.randomUUID();
  const snapshot = path.join(directory, "response_all_departments.json");
  const pending = path.join(directory, `response-${runId}.pending.json`);
  const backup = path.join(backupDir, `saleslogs-before-${runId}.ejson.gz`);
  const statusFile = path.join(directory, "last-run.json");
  const status = { status: "running", startedAt, month, snapshot, backup };
  atomicJson(statusFile, status);
  try {
    if (!env.MONGO_URL)
      throw new Error("MONGO_URL is required for the SalesLogs upsert.");
    const authentication = await session.renew();
    const payload = await fetch({ session, month });
    validateSnapshot(payload, { month });
    atomicJson(pending, payload);
    const importer = path.join(
      ROOT,
      "scripts/import_response_all_departments.py",
    );
    const args = [importer, "--input", pending, "--require-fresh"];
    await command(python, [...args, "--dry-run"], env);
    await command(python, [...args, "--backup", backup], env);
    if (fs.existsSync(snapshot))
      fs.copyFileSync(
        snapshot,
        path.join(directory, "response_all_departments.previous.json"),
      );
    fs.renameSync(pending, snapshot);
    const result = {
      ...status,
      status: "succeeded",
      finishedAt: new Date().toISOString(),
      tokenExpiresAt: authentication.tokenExpiresAt,
      counts: Object.fromEntries(
        Object.entries(payload.departments).map(([name, dept]) => [
          name,
          dept.data.list.length,
        ]),
      ),
    };
    atomicJson(statusFile, result);
    console.log(JSON.stringify(result));
    return result;
  } catch (error) {
    atomicJson(statusFile, {
      ...status,
      status:
        error.code === "SALESLOGS_AUTH_REQUIRED"
          ? "authentication_required"
          : "failed",
      finishedAt: new Date().toISOString(),
      message: error.message,
    });
    throw error;
  }
}
async function main() {
  const session = configuredSession();
  const python =
    process.env.SALESLOGS_PYTHON ||
    path.join(
      ROOT,
      process.platform === "win32"
        ? "venv/Scripts/python.exe"
        : "venv/bin/python",
    );
  return run({
    session,
    python,
    directory: path.resolve(
      ROOT,
      process.env.SALESLOGS_DATA_DIR || ".runtime/saleslogs",
    ),
  });
}
module.exports = { run, main };
if (require.main === module)
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
