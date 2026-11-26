/**
 * Where to reach the project team, from `VITE_CONTACT_URL` at build time: a
 * `https://wa.me/<number>` or `mailto:` link. Configuration, not code, so the
 * number is in netlify.toml and never in the repository's source. Empty hides
 * every contact line in the UI rather than showing a dead link.
 */
export const contactUrl: string = (import.meta.env.VITE_CONTACT_URL ?? '').trim()
