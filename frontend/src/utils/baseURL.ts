/**
 * Frontend ↔ backend connection.
 *
 * `BASE_URL` defaults to an empty string so every request is sent to the
 * same origin that served the SPA bundle. The Vite dev server (and any
 * reverse proxy in production) then forwards those paths to the FastAPI
 * backend — see `vite.config.ts` for the dev proxy map.
 *
 * This makes the SSH-tunnel workflow trivial: only one tunnel is needed
 * (`ssh -L 8880:localhost:5173 …`) and the laptop's browser talks only
 * to its own localhost.
 *
 * Override at build time with `VITE_API_URL=http://some.host:port` if
 * the backend lives on a different origin (e.g. when serving the built
 * `dist/` from a separate static host).
 */

const DEFAULT_BASE_URL = ""

export const BASE_URL: string =
  (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, "") ??
  DEFAULT_BASE_URL

/**
 * Prepend the API base URL to a relative path. Paths are expected to
 * start with a leading slash (e.g. "/manifest.json",
 * "/runs/<id>/calibrated_temperature/foo.npz").
 */
export function withBaseUrl(path: string): string {
  if (!path) return BASE_URL
  if (/^https?:\/\//.test(path)) return path
  const normalised = path.startsWith("/") ? path : `/${path}`
  return `${BASE_URL}${normalised}`
}
