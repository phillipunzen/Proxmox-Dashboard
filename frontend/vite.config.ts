import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		tailwindcss(),
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},
			// Pure SPA: the FastAPI backend serves index.html for every non-API route.
			adapter: adapter({ pages: 'build', assets: 'build', fallback: 'index.html' })
		})
	],
	build: {
		// ECharts core is ~500 kB; it is loaded once and cached (immutable assets).
		chunkSizeWarningLimit: 800
	},
	server: {
		// `npm run dev` talks to a locally running backend (uvicorn on :8000).
		proxy: {
			'/api': { target: process.env.API_TARGET ?? 'http://localhost:8000', changeOrigin: true }
		}
	}
});
