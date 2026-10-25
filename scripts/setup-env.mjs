// Creates .env from .env.example with a fresh BETTER_AUTH_SECRET. Never overwrites.
import { randomBytes } from "node:crypto";
import { copyFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";

if (existsSync(".env")) {
  console.log(".env already exists, leaving it alone");
} else {
  copyFileSync(".env.example", ".env");
  const secret = randomBytes(32).toString("base64");
  writeFileSync(".env", readFileSync(".env", "utf8").replace(/^BETTER_AUTH_SECRET=.*$/m, `BETTER_AUTH_SECRET=${secret}`));
  console.log("created .env");
}
