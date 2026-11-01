const fs = require("node:fs");
const path = require("node:path");
const https = require("node:https");
const zlib = require("node:zlib");
const crypto = require("node:crypto");

const ROOT = path.resolve(__dirname, "..");
const APP = "https://app.saleslogs.com";
const SESSION = "https://mfa.saleslogs.com/api/authenticate/session-token";
class AuthenticationError extends Error {
  constructor() {
    super(
      "SalesLogs authentication expired or was revoked. Configure a fresh SALESLOGS_COOKIE_FILE or SALESLOGS_COOKIE.",
    );
    this.code = "SALESLOGS_AUTH_REQUIRED";
  }
}
function claims(token) {
  try {
    return JSON.parse(Buffer.from(token.split(".")[1], "base64url"));
  } catch {
    return {};
  }
}
function transport({ url, method = "GET", headers = {}, body = "" }) {
  const target = new URL(url);
  if (
    target.protocol !== "https:" ||
    !["app.saleslogs.com", "mfa.saleslogs.com"].includes(target.hostname)
  )
    throw new Error("Unsupported SalesLogs origin.");
  return new Promise((resolve, reject) => {
    const req = https.request(target, { method, headers }, (res) => {
      let stream = res;
      const enc = res.headers["content-encoding"];
      if (enc === "gzip") stream = res.pipe(zlib.createGunzip());
      if (enc === "deflate") stream = res.pipe(zlib.createInflate());
      if (enc === "br") stream = res.pipe(zlib.createBrotliDecompress());
      const chunks = [];
      let size = 0;
      stream.on("data", (chunk) => {
        size += chunk.length;
        if (size > 128 * 1024 * 1024) {
          stream.destroy();
          req.destroy();
          reject(new Error("SalesLogs response exceeded the size limit."));
        } else chunks.push(chunk);
      });
      stream.on("end", () =>
        resolve({
          status: res.statusCode,
          headers: res.headers,
          body: Buffer.concat(chunks).toString("utf8"),
        }),
      );
      stream.on("error", () =>
        reject(new Error("SalesLogs response could not be read.")),
      );
      res.on("error", () =>
        reject(new Error("SalesLogs response could not be read.")),
      );
    });
    req.on("error", () =>
      reject(new Error("SalesLogs request failed or timed out.")),
    );
    req.setTimeout(45000, () => req.destroy());
    req.end(body);
  });
}
function atomicJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const temp = `${file}.${process.pid}.${crypto.randomUUID()}.tmp`;
  fs.writeFileSync(temp, JSON.stringify(value, null, 2), {
    mode: 0o600,
    flag: "wx",
  });
  fs.renameSync(temp, file);
  if (process.platform !== "win32") fs.chmodSync(file, 0o600);
}
class SaleslogsSession {
  constructor({
    file,
    seed = "",
    request = transport,
    now = () => Date.now(),
  }) {
    this.file = file;
    this.request = request;
    this.now = now;
    this.state = fs.existsSync(file)
      ? JSON.parse(fs.readFileSync(file, "utf8"))
      : { cookies: [] };
    if (!Array.isArray(this.state.cookies))
      throw new Error("Invalid SalesLogs session file.");
    if (seed) {
      const fingerprint = crypto
        .createHash("sha256")
        .update(seed.trim())
        .digest("hex");
      if (fingerprint !== this.state.seedFingerprint) {
        this.state.cookies = [];
        for (const part of seed.trim().split(";")) {
          const i = part.indexOf("=");
          if (i > 0)
            this.set(
              part.slice(0, i).trim(),
              part.slice(i + 1).trim(),
              "saleslogs.com",
              "/",
              null,
            );
        }
        this.state.seedFingerprint = fingerprint;
        this.save();
      }
    }
  }
  set(name, value, domain, cookiePath, expires, hostOnly = false) {
    this.state.cookies = this.state.cookies.filter(
      (c) => !(c.name === name && c.domain === domain && c.path === cookiePath),
    );
    if (expires == null || expires > this.now())
      this.state.cookies.push({
        name,
        value,
        domain,
        path: cookiePath,
        expires,
        hostOnly,
      });
  }
  cookies(url) {
    const target = new URL(url);
    return this.state.cookies
      .filter(
        (c) =>
          (c.expires == null || c.expires > this.now()) &&
          (target.hostname === c.domain ||
            (!c.hostOnly && target.hostname.endsWith("." + c.domain))) &&
          (target.pathname === c.path ||
            target.pathname.startsWith(
              c.path.endsWith("/") ? c.path : c.path + "/",
            )),
      )
      .sort(
        (a, b) =>
          b.path.length - a.path.length || b.domain.length - a.domain.length,
      )
      .filter(
        (cookie, index, all) =>
          all.findIndex((other) => other.name === cookie.name) === index,
      )
      .map((c) => `${c.name}=${c.value}`)
      .join("; ");
  }
  save() {
    this.state.updatedAt = new Date(this.now()).toISOString();
    atomicJson(this.file, this.state);
  }
  absorb(headers, url) {
    const target = new URL(url);
    const values = headers["set-cookie"] || [];
    for (const header of typeof values === "string" ? [values] : values) {
      const [pair, ...attributes] = header.split(";"),
        i = pair.indexOf("=");
      if (i < 1) continue;
      const attrs = Object.fromEntries(
        attributes.map((value) => {
          const n = value.indexOf("=");
          return n < 0
            ? [value.trim().toLowerCase(), true]
            : [
                value.slice(0, n).trim().toLowerCase(),
                value.slice(n + 1).trim(),
              ];
        }),
      );
      const domain = String(attrs.domain || target.hostname)
        .replace(/^\./, "")
        .toLowerCase();
      if (target.hostname !== domain && !target.hostname.endsWith("." + domain))
        continue;
      if (
        !["saleslogs.com", "app.saleslogs.com", "mfa.saleslogs.com"].includes(
          domain,
        )
      )
        continue;
      let expires = attrs.expires ? Date.parse(attrs.expires) : null;
      if (
        attrs["max-age"] !== undefined &&
        Number.isFinite(Number(attrs["max-age"]))
      )
        expires = this.now() + Number(attrs["max-age"]) * 1000;
      if (!Number.isFinite(expires)) expires = null;
      this.set(
        pair.slice(0, i).trim(),
        pair.slice(i + 1).trim(),
        domain,
        typeof attrs.path === "string" && attrs.path.startsWith("/")
          ? attrs.path
          : "/",
        expires,
        !attrs.domain,
      );
    }
    if (values.length) this.save();
  }
  auth() {
    const cookie = this.cookies(APP + "/au/v1/Live")
      .split("; ")
      .find((c) => c.startsWith("AUTHSALESLOGS="));
    if (!cookie) return {};
    try {
      return JSON.parse(
        decodeURIComponent(cookie.slice("AUTHSALESLOGS=".length)),
      );
    } catch {
      return {};
    }
  }
  async send(url, method = "GET", body = "") {
    const response = await this.request({
      url,
      method,
      body,
      headers: {
        Accept: "application/json",
        "Accept-Encoding": "gzip, deflate, br",
        "Content-Type": "application/json; charset=UTF-8",
        ...(method === "POST"
          ? { "Content-Length": Buffer.byteLength(body) }
          : {}),
        Origin: APP,
        Referer: APP + "/au/v1/Live",
        "X-Requested-With": "XMLHttpRequest",
        Cookie: this.cookies(url),
      },
    });
    this.absorb(response.headers || {}, url);
    return response;
  }
  async renew() {
    const original = this.auth(),
      previous = claims(original.tokenId);
    let response;
    if (original.tokenId)
      response = await this.send(
        APP +
          "/au/v1/api/GetUser?tokenId=" +
          encodeURIComponent(original.tokenId),
        "POST",
      );
    const read = (value) => {
      try {
        return JSON.parse(value.body);
      } catch {
        return {};
      }
    };
    let next = response?.status === 200 ? read(response).tokenId : null;
    if (!next) {
      if (response?.status >= 500)
        throw new Error(
          "SalesLogs renewal service is temporarily unavailable.",
        );
      const recovered = await this.send(SESSION);
      if (recovered.status === 200) next = read(recovered).token;
      else if (recovered.status >= 500)
        throw new Error(
          "SalesLogs session service is temporarily unavailable.",
        );
    }
    const decoded = claims(next);
    if (!next || !decoded.exp || decoded.exp * 1000 <= this.now())
      throw new AuthenticationError();
    const beforeUser = previous.UserId || previous.unique_name,
      afterUser = decoded.UserId || decoded.unique_name;
    if (beforeUser && afterUser && beforeUser !== afterUser)
      throw new Error("SalesLogs renewal changed the account identity.");
    // A server-issued token is persisted. Decoded claims are used only for scheduling.
    this.set(
      "AUTHSALESLOGS",
      JSON.stringify({ tokenId: next, role: decoded.role || original.role }),
      "saleslogs.com",
      "/",
      null,
    );
    this.state.lastRefreshAt = new Date(this.now()).toISOString();
    this.save();
    return {
      authenticated: true,
      renewed: next !== original.tokenId,
      tokenExpiresAt: new Date(decoded.exp * 1000).toISOString(),
    };
  }
  async getLive(body) {
    const token = this.auth().tokenId;
    if (!token || !(claims(token).exp * 1000 > this.now() + 10 * 60 * 1000))
      await this.renew();
    const url = APP + "/au/v1/Live/GetLiveAllData";
    let response = await this.send(url, "POST", JSON.stringify(body));
    if ([301, 302, 303, 307, 308, 401, 403].includes(response.status)) {
      await this.renew();
      response = await this.send(url, "POST", JSON.stringify(body));
    }
    if ([301, 302, 303, 307, 308, 401, 403].includes(response.status))
      throw new AuthenticationError();
    if (response.status !== 200)
      throw new Error(
        `SalesLogs data request returned HTTP ${response.status}.`,
      );
    try {
      return JSON.parse(response.body.replace(/^\uFEFF/, ""));
    } catch {
      throw new Error(
        "SalesLogs did not return JSON; the snapshot was not updated.",
      );
    }
  }
}
function configuredSession() {
  if (fs.existsSync(path.join(ROOT, ".env")))
    process.loadEnvFile(path.join(ROOT, ".env"));
  const seed = process.env.SALESLOGS_COOKIE_FILE
    ? fs
        .readFileSync(
          path.resolve(ROOT, process.env.SALESLOGS_COOKIE_FILE),
          "utf8",
        )
        .trim()
    : process.env.SALESLOGS_COOKIE || "";
  return new SaleslogsSession({
    file: path.resolve(
      ROOT,
      process.env.SALESLOGS_SESSION_FILE || ".runtime/saleslogs/session.json",
    ),
    seed,
  });
}
async function main() {
  const statusFile = path.resolve(
    ROOT,
    process.env.SALESLOGS_SESSION_STATUS_FILE ||
      ".runtime/saleslogs/session-status.json",
  );
  try {
    const result = {
      status: "authenticated",
      checkedAt: new Date().toISOString(),
      ...(await configuredSession().renew()),
    };
    atomicJson(statusFile, result);
    console.log(JSON.stringify(result));
  } catch (error) {
    atomicJson(statusFile, {
      status:
        error.code === "SALESLOGS_AUTH_REQUIRED"
          ? "authentication_required"
          : "failed",
      checkedAt: new Date().toISOString(),
      message: error.message,
    });
    throw error;
  }
}
module.exports = {
  SaleslogsSession,
  AuthenticationError,
  claims,
  atomicJson,
  configuredSession,
  ROOT,
  main,
};
if (require.main === module)
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
