// Copies the static page into build/. Cloudways runs `npm run build` and then
// serves the Output Directory (build), so this keeps the deployed bundle free of
// package.json, node_modules and this script.
import { cp, mkdir, rm } from "node:fs/promises";

const FILES = ["index.html", "config.js"];

await rm("build", { recursive: true, force: true });
await mkdir("build", { recursive: true });
for (const file of FILES) {
  await cp(file, `build/${file}`);
}

console.log(`built ${FILES.length} files into build/: ${FILES.join(", ")}`);
