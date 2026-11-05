// Production build of the WebUI into dist/, served by the API.
import { rmSync } from "node:fs";
import tailwind from "bun-plugin-tailwind";

// Hashed file names change every build; start from an empty dist/ so old bundles do not pile up.
rmSync("./dist", { recursive: true, force: true });

const result = await Bun.build({
  entrypoints: ["./src/index.html"],
  outdir: "./dist",
  plugins: [tailwind],
  minify: true,
  sourcemap: "linked",
  target: "browser",
  // Absolute asset URLs: the SPA serves index.html for deep links such as /p/forge/launch.
  publicPath: "/",
  naming: {
    entry: "[dir]/[name].[ext]",
    chunk: "assets/[name]-[hash].[ext]",
    asset: "assets/[name]-[hash].[ext]",
  },
});

if (!result.success) {
  for (const message of result.logs) console.error(message);
  process.exit(1);
}
console.log(`built ${result.outputs.length} files into dist/`);
