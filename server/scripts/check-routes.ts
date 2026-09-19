import { buildApp } from "../src/app.js";
import { TokenManager } from "../src/auth/token-manager.js";
import { openDatabase, ensureSchema } from "../src/db/client.js";
import pino from "pino";

const logger = pino({ level: "silent" });
const settings = {
  enabled: true,
  host: ["127.0.0.1"],
  port: 0,
  mode: "development" as const,
  secureCookie: false,
};
const tokenManager = TokenManager.open("data/webui.json", logger);
const db = openDatabase("data/MaiBot.db", logger);
ensureSchema(db, logger);
const rootDir = process.cwd().replace(/[/\\]server$/, "");
const app = buildApp({
  settings, tokenManager, logger,
  rootDir, db, serveDashboard: false,
});

app.ready().then(() => {
  const routes = app.printRoutes({ commonPrefix: false });
  const dataRoutes = routes.split("\n").filter((line) =>
    line.includes("person") || line.includes("jargon") || line.includes("statistics") ||
    line.includes("expression") || line.includes("/ws") || line.includes("health")
  );
  console.log("data routes:", dataRoutes.length);
  for (const line of dataRoutes) console.log(" ", line.trim());
  console.log("total routes:", routes.split("\n").filter((l) => l.trim()).length);
  process.exit(0);
});
