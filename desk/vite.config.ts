import { createHash } from "crypto"
import { readdirSync, readFileSync } from "fs"
import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig, type Plugin } from "vite"
import { viteSingleFile } from "vite-plugin-singlefile"

// Build id = hash of the sources, not a timestamp: CI rebuilds desk.html and requires the
// committed file to match, so the id must be reproducible. An open Desk compares its own
// <meta name="desk-build"> with the deployed page's and offers a reload (App.tsx Banners).
const files = (dir: string): string[] => readdirSync(dir, { withFileTypes: true })
  .flatMap(e => (e.isDirectory() ? files(path.join(dir, e.name)) : [path.join(dir, e.name)])).sort()
const BUILD = createHash("sha256")
  .update([...files(path.resolve(__dirname, "src")), path.resolve(__dirname, "index.html")]
    .map(f => path.relative(__dirname, f) + "\0" + readFileSync(f, "utf8")).join("\0"))
  .digest("hex").slice(0, 12)

const buildMeta = (): Plugin => ({
  name: "desk-build-meta",
  transformIndexHtml: html => html.replace("</head>", `  <meta name="desk-build" content="${BUILD}" />\n  </head>`),
})

// One self-contained HTML file (no CDN, no extra requests): company-api serves
// app/static/desk.html. `npm run build` writes it; CI checks it matches the source.
export default defineConfig({
  plugins: [react(), tailwindcss(), viteSingleFile(), buildMeta()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: { proxy: Object.fromEntries(["/desk", "/agents", "/tasks", "/company", "/dispatch", "/control", "/approvals"]
    .map(p => [p, "http://localhost:8765"])) },
})
