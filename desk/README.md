# Founder Desk source

React + TypeScript + Tailwind v4 + shadcn/ui. Built into one self-contained file,
`../app/static/desk.html`, which company-api serves unchanged at `/` and `/ui`. Commit the built file.

```bash
npm ci
npm run dev     # http://localhost:5173, proxies the API to localhost:8765
npm run build   # rewrites ../app/static/desk.html; CI fails if it is stale
```

Add components with `npx shadcn@latest add <name>`. Data contract: `src/lib/api.ts`
(`/desk/state` from `app/desk.py`; route scope `DESK` in `app/api_auth.py`).
