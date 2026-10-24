# Frontend (SvelteKit SPA)

Requires Node >= 22.17. Without a local Node 22 use Docker:

```bash
docker run --rm -it -v "$PWD":/app -w /app -p 5173:5173 -e API_TARGET=http://host.docker.internal:8000 --add-host host.docker.internal:host-gateway node:22-alpine npm run dev -- --host
```

`npm run dev` proxies `/api` to the backend (`API_TARGET`, default `http://localhost:8000`).
`npm run check` runs the type checker, `npm run build` writes the static app to `build/`.
