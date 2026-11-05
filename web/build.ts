// Production build of the WebUI into dist/, served by the API.
import tailwind from "bun-plugin-tailwind";

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
