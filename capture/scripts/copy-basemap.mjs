// Copy the Harare basemap archive from the web app into this app's public/.
//
// The archive has one home in the repository, web/public/harare.pmtiles, so a
// refresh (see infra/README.md#basemap-tiles) is a single file replacement.
// Committing a second copy here would mean two 6 MB binaries to keep in step
// and both in git history forever. Instead this runs before `dev` and `build`
// (npm's pre-script hooks in package.json), and the copy is gitignored.
//
// Netlify clones the whole repository even with the base directory set to
// capture/, so the source exists at build time there too.
import { copyFileSync, existsSync, mkdirSync, statSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const source = resolve(here, '../../web/public/harare.pmtiles')
const target = resolve(here, '../public/harare.pmtiles')

if (!existsSync(source)) {
  // Not fatal: the app falls back to a blank background, which is the state
  // this script exists to avoid, so say so loudly.
  console.warn(`[basemap] ${source} not found; the capture map will have no basemap.`)
  process.exit(0)
}

const fresh = existsSync(target) && statSync(target).mtimeMs >= statSync(source).mtimeMs
if (fresh) {
  console.log('[basemap] public/harare.pmtiles is up to date')
} else {
  mkdirSync(dirname(target), { recursive: true })
  copyFileSync(source, target)
  console.log('[basemap] copied web/public/harare.pmtiles -> public/harare.pmtiles')
}
