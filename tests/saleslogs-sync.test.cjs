const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  ROOT,
  SaleslogsSession,
  AuthenticationError,
} = require("../data-saleslog/session");
const {
  DEPARTMENTS,
  currentMonth,
  options,
  fetchSnapshot,
  validateSnapshot,
  parseCsv,
} = require("../data-saleslog/index");
const { run } = require("../data-saleslog/run-hourly");
const runtime = path.join(ROOT, ".runtime");
fs.mkdirSync(runtime, { recursive: true });
const stamp = Date.now();
const token = (seconds, user = "fixture") =>
  "header." +
  Buffer.from(
    JSON.stringify({
      UserId: user,
      exp: Math.floor(stamp / 1000) + seconds,
      role: "SM",
    }),
  ).toString("base64url") +
  ".fixture";
const auth = (value) =>
  `AUTHSALESLOGS=${JSON.stringify({ tokenId: value, role: "SM" })}; ASP.NET_SessionId=original`;
function directory(t) {
  const dir = fs.mkdtempSync(path.join(runtime, "saleslogs-test-"));
  t.after(() => {
    assert(
      path
        .resolve(dir)
        .startsWith(path.resolve(runtime) + path.sep + "saleslogs-test-"),
    );
    fs.rmSync(dir, { recursive: true, force: true });
  });
  return dir;
}
const reply = (body, status = 200, headers = {}) => ({
  status,
  headers,
  body: JSON.stringify(body),
});
const snapshot = () => ({
  fetchedAt: new Date().toISOString(),
  targetMonth: currentMonth(),
  departments: Object.fromEntries(
    DEPARTMENTS.map((d) => [
      d.name,
      { id: d.id, data: { list: [{ id: d.key }] } },
    ]),
  ),
});

test("renewal persists only a server-issued token and Set-Cookie rotations across restarts", async (t) => {
  const file = path.join(directory(t), "session.json"),
    seed = auth(token(60));
  const fresh = token(3600);
  const session = new SaleslogsSession({
    file,
    seed,
    now: () => stamp,
    request: async (request) => {
      assert.equal(new URL(request.url).pathname, "/au/v1/api/GetUser");
      assert.equal(request.method, "POST");
      return reply({ tokenId: fresh }, 200, {
        "set-cookie": ["ASP.NET_SessionId=rotated; Path=/; HttpOnly; Secure"],
      });
    },
  });
  const result = await session.renew();
  assert.equal(result.renewed, true);
  assert.equal(session.auth().tokenId, fresh);
  assert.match(
    session.cookies("https://app.saleslogs.com/au/v1/Live"),
    /ASP.NET_SessionId=rotated/,
  );
  assert.doesNotMatch(
    session.cookies("https://app.saleslogs.com/au/v1/Live"),
    /ASP.NET_SessionId=original/,
  );
  const restored = new SaleslogsSession({ file, seed });
  assert.equal(
    restored.auth().tokenId,
    fresh,
    "the old seed must not reset a renewed session",
  );
  if (process.platform !== "win32")
    assert.equal(fs.statSync(file).mode & 0o777, 0o600);
});

test("expired/revoked sessions fail clearly and never manufacture a replacement token", async (t) => {
  const file = path.join(directory(t), "session.json"),
    expired = token(-100);
  const session = new SaleslogsSession({
    file,
    seed: auth(expired),
    now: () => stamp,
    request: async () => reply({}, 401),
  });
  await assert.rejects(session.renew(), AuthenticationError);
  assert.equal(session.auth().tokenId, expired);
});

test("renewal can recover a genuine HTTP-only session but rejects an account identity change", async (t) => {
  const file = path.join(directory(t), "session.json");
  let requests = 0;
  const session = new SaleslogsSession({
    file,
    seed: auth(token(-1)),
    now: () => stamp,
    request: async (request) => {
      requests++;
      return new URL(request.url).hostname === "mfa.saleslogs.com"
        ? reply({ token: token(3600) })
        : reply({}, 401);
    },
  });
  await session.renew();
  assert.equal(requests, 2);
  session.request = async () =>
    reply({ tokenId: token(3600, "different-user") });
  await assert.rejects(session.renew(), /account identity/);
});

test("cookie deletion, domain/path scope and reseeding are respected", (t) => {
  const file = path.join(directory(t), "session.json");
  const session = new SaleslogsSession({
    file,
    seed: auth(token(3600)),
    now: () => stamp,
  });
  session.absorb(
    {
      "set-cookie": [
        "private=1; Path=/private",
        "bad=1; Domain=example.com",
        "dead=1; Max-Age=0",
      ],
    },
    "https://app.saleslogs.com/",
  );
  assert.doesNotMatch(
    session.cookies("https://app.saleslogs.com/au/v1/Live"),
    /private|bad|dead/,
  );
  assert.match(
    session.cookies("https://app.saleslogs.com/private/page"),
    /private=1/,
  );
  assert.doesNotMatch(
    session.cookies("https://mfa.saleslogs.com/private/page"),
    /private=1/,
  );
  assert.equal(
    new SaleslogsSession({ file, seed: auth(token(7200)) }).auth().tokenId,
    token(7200),
  );
});

test("data authentication errors renew and retry once without accepting HTML/login responses", async (t) => {
  const file = path.join(directory(t), "session.json");
  let calls = 0;
  const session = new SaleslogsSession({
    file,
    seed: auth(token(7200)),
    now: () => stamp,
    request: async (request) => {
      if (new URL(request.url).pathname.endsWith("GetUser"))
        return reply({ tokenId: token(3600) });
      return ++calls === 1 ? reply({}, 401) : reply({ list: [] });
    },
  });
  assert.deepEqual(await session.getLive({}), { list: [] });
  assert.equal(calls, 2);
  session.request = async () => ({
    status: 200,
    headers: {},
    body: "<html>login</html>",
  });
  await assert.rejects(session.getLive({}), /did not return JSON/);
});

test("month selection follows Melbourne rollover and excludes the separate DENZA department", () => {
  assert.equal(currentMonth(new Date("2026-10-31T13:00:00Z")), 202611);
  assert.equal(options([]).departments.length, 2);
  assert.throws(() => options(["--sites=denza-melbourne"]), /Only/);
  assert.throws(() => options(["202613"]), /Invalid/);
  assert.deepEqual(parseCsv('id,Comment\r\n1,"a,b\nsecond line"\r\n'), [
    { id: "1", Comment: "a,b\nsecond line" },
  ]);
});

test("a partial department failure never returns a successful snapshot", async () => {
  let calls = 0;
  await assert.rejects(
    fetchSnapshot({
      session: {
        async getLive(body) {
          assert(DEPARTMENTS.some((d) => d.id === body.id));
          if (++calls === 2) throw new Error("upstream failed");
          return { listCsv: "id,Name\n1,Saved" };
        },
      },
    }),
    /upstream failed/,
  );
  assert.equal(calls, 2);
});

test("snapshot checks reject stale, empty, extra-department and wrong-month payloads", () => {
  const data = snapshot();
  validateSnapshot(data);
  assert.throws(
    () => validateSnapshot({ ...data, fetchedAt: "2020-01-01T00:00:00Z" }),
    /stale/,
  );
  assert.throws(() => validateSnapshot(data, { month: 202001 }), /month/);
  assert.throws(
    () =>
      validateSnapshot({
        ...data,
        departments: { ...data.departments, "Denza Melbourne - New": {} },
      }),
    /DENZA/,
  );
  assert.throws(
    () => validateSnapshot({ ...data, departments: {} }),
    /Both BYD/,
  );
});

test("hourly sync verifies the dry run and backed-up upsert before publishing the snapshot", async (t) => {
  const dir = directory(t),
    file = path.join(dir, "response_all_departments.json");
  fs.writeFileSync(file, "previous");
  const commands = [];
  const data = snapshot();
  const result = await run({
    session: {
      async renew() {
        return { tokenExpiresAt: "future" };
      },
    },
    directory: dir,
    python: "python",
    env: { MONGO_URL: "fixture" },
    fetch: async () => data,
    command: async (python, args) => {
      commands.push(args);
      assert.equal(fs.readFileSync(file, "utf8"), "previous");
    },
  });
  assert.equal(commands.length, 2);
  assert(commands[0].includes("--dry-run"));
  assert(commands[1].includes("--backup"));
  assert(
    commands.every(
      (args) =>
        args.includes("--require-fresh") && !args.includes("--include-denza"),
    ),
  );
  assert.equal(result.status, "succeeded");
  assert.deepEqual(JSON.parse(fs.readFileSync(file)), data);
  assert.equal(
    fs.readFileSync(
      path.join(dir, "response_all_departments.previous.json"),
      "utf8",
    ),
    "previous",
  );
});

test("failed import leaves the previous snapshot intact and records failure", async (t) => {
  const dir = directory(t),
    file = path.join(dir, "response_all_departments.json");
  fs.writeFileSync(file, "previous");
  await assert.rejects(
    run({
      session: {
        async renew() {
          return {};
        },
      },
      directory: dir,
      python: "python",
      env: { MONGO_URL: "fixture" },
      fetch: async () => snapshot(),
      command: async () => {
        throw new Error("import failed");
      },
    }),
    /import failed/,
  );
  assert.equal(fs.readFileSync(file, "utf8"), "previous");
  assert.equal(
    JSON.parse(fs.readFileSync(path.join(dir, "last-run.json"))).status,
    "failed",
  );
});
