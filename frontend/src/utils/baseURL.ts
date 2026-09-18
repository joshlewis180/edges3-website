/**
 * Frontend ↔ backend connection.
 *
 * `BASE_URL` is read from the Vite env (`VITE_API_URL`) at build time, with
 * a fallback for local development. The same bundle works both on
 * localhost and on the SSH cluster via port-forwarding (see the project
 * README for the `ssh -L` workflow).
 *
 *   Local:   leave VITE_API_URL unset → http://127.0.0.1:8000
 *   Cluster: set VITE_API_URL=http://127.0.0.1:8000 (after ssh -L)
 */

const DEFAULT_BASE_URL = "http://127.0.0.1:8000"

export const BASE_URL: string =
  (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, "") ||
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
